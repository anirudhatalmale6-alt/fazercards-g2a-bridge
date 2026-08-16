"""The one HTTP client every adapter goes through.

What it guarantees:

* **Exponential back-off with jitter** on transient failures (429, 5xx, timeouts,
  connection resets).  Jitter matters -- without it a batch of parallel calls
  retries in lockstep and re-creates the burst that caused the 429.
* **Retry-After is obeyed.**  If the server tells us how long to wait we wait
  that long instead of guessing.
* **4xx other than 429/408 are never retried.**  Retrying a 400 just burns
  quota; retrying a 401 can get an account flagged.
* **Non-idempotent calls opt in.**  A POST that buys a key is only retried when
  the caller passes an idempotency key, so a retry can never buy twice.
* **Every attempt is logged** to ``api_calls`` with secrets scrubbed.
"""

from __future__ import annotations

import json
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import httpx

from app.config import Settings, get_settings
from app.http.ratelimit import TokenBucket
from app.logging_conf import get_logger

logger = get_logger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
RETRYABLE_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.PoolTimeout,
)
_MAX_LOGGED_BODY = 4000


class ApiError(RuntimeError):
    """A call that failed in a way the caller has to deal with."""

    def __init__(
        self,
        message: str,
        *,
        service: str,
        status_code: int | None = None,
        payload: Any = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.service = service
        self.status_code = status_code
        self.payload = payload
        self.retryable = retryable

    def __str__(self) -> str:  # pragma: no cover - formatting only
        base = super().__str__()
        return f"[{self.service}] {base}" + (
            f" (HTTP {self.status_code})" if self.status_code else ""
        )


class RateLimitedError(ApiError):
    """429 that survived every retry."""


class AuthError(ApiError):
    """401/403 -- retrying will not help, the credentials or scopes are wrong."""


@dataclass
class ApiCallRecord:
    """What we log about one attempt.  Bodies are truncated and scrubbed."""

    service: str
    method: str
    url: str
    attempt: int
    status_code: int | None = None
    duration_ms: int | None = None
    request_body: str | None = None
    response_body: str | None = None
    error: str | None = None
    correlation_id: str | None = None


CallSink = Callable[[ApiCallRecord], None]


def _null_sink(_record: ApiCallRecord) -> None:
    return None


def scrub(value: Any, secret_keys: tuple[str, ...]) -> Any:
    """Recursively replace anything that looks like a credential."""
    lowered = {k.lower() for k in secret_keys}
    if isinstance(value, Mapping):
        return {
            k: ("***" if k.lower() in lowered else scrub(v, secret_keys))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [scrub(v, secret_keys) for v in value]
    return value


def _truncate(text: str | None) -> str | None:
    if text is None:
        return None
    if len(text) <= _MAX_LOGGED_BODY:
        return text
    return text[:_MAX_LOGGED_BODY] + f"...<truncated {len(text) - _MAX_LOGGED_BODY} chars>"


@dataclass
class BaseApiClient:
    """Shared transport for every upstream API.

    Subclasses supply the base URL, auth headers and error decoding; they do not
    re-implement retrying, and so cannot get it subtly wrong in one place.
    """

    service: str
    base_url: str
    settings: Settings = field(default_factory=get_settings)
    client: httpx.Client | None = None
    bucket: TokenBucket | None = None
    call_sink: CallSink = _null_sink
    sleep: Callable[[float], None] = time.sleep
    _rng: random.Random = field(default_factory=random.Random)

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = httpx.Client(
                base_url=self.base_url,
                timeout=self.settings.http_timeout_seconds,
                follow_redirects=True,
            )

    # ------------------------------------------------------------- overrides
    def auth_headers(self) -> dict[str, str]:
        return {}

    def on_auth_failure(self) -> None:
        """Hook for token refresh.  Called once before the retry of a 401."""
        return None

    def decode_error(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return _truncate(response.text) or response.reason_phrase
        if isinstance(payload, dict):
            for key in ("error", "message", "detail", "error_description"):
                if key in payload and isinstance(payload[key], str):
                    code = payload.get("code")
                    return f"{payload[key]}" + (f" ({code})" if code else "")
        return _truncate(json.dumps(payload)) or response.reason_phrase

    # ---------------------------------------------------------------- helpers
    def _backoff_delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            # Server knows better than our formula.  Cap it so a silly
            # Retry-After cannot wedge a worker for an hour.
            return min(retry_after, self.settings.retry_max_delay_seconds)
        raw = self.settings.retry_base_delay_seconds * (2 ** (attempt - 1))
        capped = min(raw, self.settings.retry_max_delay_seconds)
        jitter = capped * self.settings.retry_jitter
        return max(0.0, capped + self._rng.uniform(-jitter, jitter))

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        value = response.headers.get("retry-after")
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            try:
                from email.utils import parsedate_to_datetime

                import datetime as _dt

                when = parsedate_to_datetime(value)
                delta = (when - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
                return max(0.0, delta)
            except Exception:
                return None

    # ------------------------------------------------------------------ core
    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: Any = None,
        data: Any = None,
        headers: dict[str, str] | None = None,
        idempotency_key: str | None = None,
        allow_retry: bool | None = None,
        expected: tuple[int, ...] = (200, 201, 202, 204),
        correlation_id: str | None = None,
    ) -> Any:
        """Perform a call and return the decoded JSON body (or None for 204)."""
        assert self.client is not None
        method = method.upper()
        correlation_id = correlation_id or uuid.uuid4().hex[:16]

        if allow_retry is None:
            # GET/HEAD are safe by definition; a mutating call is only safe to
            # replay when the server will de-duplicate it for us.
            allow_retry = method in {"GET", "HEAD", "OPTIONS"} or idempotency_key is not None

        scrubbed_request = _truncate(
            json.dumps(scrub(json_body, self.settings.secret_headers))
            if json_body is not None
            else None
        )

        attempt = 0
        refreshed_auth = False
        last_error: Exception | None = None

        while True:
            attempt += 1
            if self.bucket is not None:
                self.bucket.acquire()

            send_headers = {**self.auth_headers(), **(headers or {})}
            if idempotency_key:
                send_headers.setdefault("Idempotency-Key", idempotency_key)

            record = ApiCallRecord(
                service=self.service,
                method=method,
                url=str(httpx.URL(self.base_url).join(path)),
                attempt=attempt,
                request_body=scrubbed_request,
                correlation_id=correlation_id,
            )
            started = time.monotonic()

            try:
                response = self.client.request(
                    method,
                    path,
                    params=params,
                    json=json_body,
                    data=data,
                    headers=send_headers,
                )
            except RETRYABLE_EXCEPTIONS as exc:
                record.duration_ms = int((time.monotonic() - started) * 1000)
                record.error = f"{type(exc).__name__}: {exc}"
                self._log(record)
                last_error = exc
                if not allow_retry or attempt >= self.settings.retry_max_attempts:
                    raise ApiError(
                        f"{method} {path} failed after {attempt} attempt(s): {exc}",
                        service=self.service,
                        retryable=True,
                    ) from exc
                delay = self._backoff_delay(attempt, None)
                logger.warning(
                    "%s %s %s -> %s; retrying in %.2fs (attempt %d/%d)",
                    self.service, method, path, type(exc).__name__, delay,
                    attempt, self.settings.retry_max_attempts,
                )
                self.sleep(delay)
                continue

            record.duration_ms = int((time.monotonic() - started) * 1000)
            record.status_code = response.status_code
            record.response_body = _truncate(response.text)
            self._log(record)

            if response.status_code in expected:
                if response.status_code == 204 or not response.content:
                    return None
                try:
                    return response.json()
                except ValueError as exc:
                    raise ApiError(
                        f"{method} {path} returned non-JSON body",
                        service=self.service,
                        status_code=response.status_code,
                        payload=_truncate(response.text),
                    ) from exc

            message = self.decode_error(response)

            if response.status_code in (401, 403):
                if response.status_code == 401 and not refreshed_auth and allow_retry:
                    # Token probably expired mid-flight.  Refresh once, retry once.
                    refreshed_auth = True
                    self.on_auth_failure()
                    continue
                raise AuthError(
                    message, service=self.service, status_code=response.status_code
                )

            retryable = response.status_code in RETRYABLE_STATUS
            if not retryable or not allow_retry or attempt >= self.settings.retry_max_attempts:
                error_cls = RateLimitedError if response.status_code == 429 else ApiError
                raise error_cls(
                    f"{method} {path}: {message}",
                    service=self.service,
                    status_code=response.status_code,
                    payload=_truncate(response.text),
                    retryable=retryable,
                )

            delay = self._backoff_delay(attempt, self._retry_after(response))
            logger.warning(
                "%s %s %s -> HTTP %d; retrying in %.2fs (attempt %d/%d)",
                self.service, method, path, response.status_code, delay,
                attempt, self.settings.retry_max_attempts,
            )
            last_error = ApiError(
                message, service=self.service, status_code=response.status_code
            )
            self.sleep(delay)

        raise last_error or ApiError("unreachable", service=self.service)

    # --------------------------------------------------------------- sugar
    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> Any:
        return self.request("POST", path, **kw)

    def put(self, path: str, **kw: Any) -> Any:
        return self.request("PUT", path, **kw)

    def delete(self, path: str, **kw: Any) -> Any:
        return self.request("DELETE", path, **kw)

    def close(self) -> None:
        if self.client is not None:
            self.client.close()

    def _log(self, record: ApiCallRecord) -> None:
        try:
            self.call_sink(record)
        except Exception:  # never let logging break a live call
            logger.exception("failed to persist api_call record")
