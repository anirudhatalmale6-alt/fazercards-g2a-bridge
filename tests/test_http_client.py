"""Retry, back-off and rate-limit behaviour."""

from __future__ import annotations

import httpx
import pytest

from app.config import get_settings
from app.http.client import ApiError, AuthError, BaseApiClient, RateLimitedError, scrub
from app.http.ratelimit import TokenBucket
from app.http.sink import ListSink


def build_client(handler, *, sleeps: list[float] | None = None, service="test"):
    settings = get_settings()
    sink = ListSink()
    client = BaseApiClient(
        service=service,
        base_url="https://example.test",
        settings=settings,
        client=httpx.Client(
            transport=httpx.MockTransport(handler), base_url="https://example.test"
        ),
        call_sink=sink,
        sleep=(sleeps.append if sleeps is not None else (lambda _s: None)),
    )
    return client, sink


def test_retries_on_500_then_succeeds():
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"ok": True})

    client, sink = build_client(handler)
    assert client.get("/thing") == {"ok": True}
    assert calls["n"] == 3
    # Every attempt is logged, not just the last one.
    assert sink.attempts == 3
    assert [r.status_code for r in sink.records] == [500, 500, 200]


def test_backoff_grows_exponentially():
    sleeps: list[float] = []
    client, _ = build_client(lambda _r: httpx.Response(503), sleeps=sleeps)

    with pytest.raises(ApiError):
        client.get("/thing")

    settings = get_settings()
    assert len(sleeps) == settings.retry_max_attempts - 1
    # Each delay is drawn from base * 2^(n-1) +/- jitter, so compare envelopes
    # rather than exact values.
    for i, delay in enumerate(sleeps, start=1):
        expected = min(
            settings.retry_base_delay_seconds * (2 ** (i - 1)),
            settings.retry_max_delay_seconds,
        )
        assert expected * 0.7 <= delay <= expected * 1.3
    assert sleeps[-1] > sleeps[0]


def test_retry_after_header_is_obeyed():
    sleeps: list[float] = []
    responses = [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={})]

    def handler(_request):
        return responses.pop(0)

    client, _ = build_client(handler, sleeps=sleeps)
    client.get("/thing")
    assert sleeps == [7.0]


def test_429_that_never_clears_raises_rate_limited():
    client, _ = build_client(lambda _r: httpx.Response(429))
    with pytest.raises(RateLimitedError):
        client.get("/thing")


def test_400_is_not_retried():
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        return httpx.Response(400, json={"error": "bad request"})

    client, _ = build_client(handler)
    with pytest.raises(ApiError) as exc:
        client.get("/thing")
    assert calls["n"] == 1
    assert exc.value.status_code == 400


def test_403_raises_auth_error_without_retrying():
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        return httpx.Response(403, json={"error": "forbidden"})

    client, _ = build_client(handler)
    with pytest.raises(AuthError):
        client.get("/thing")
    assert calls["n"] == 1


def test_401_refreshes_auth_once_then_gives_up():
    calls = {"n": 0}
    refreshed = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        return httpx.Response(401, json={"error": "expired"})

    client, _ = build_client(handler)
    client.on_auth_failure = lambda: refreshed.__setitem__("n", refreshed["n"] + 1)

    with pytest.raises(AuthError):
        client.get("/thing")
    assert refreshed["n"] == 1
    assert calls["n"] == 2  # original + one retry after refresh


def test_post_without_idempotency_key_is_not_retried():
    """A blind POST retry could buy a second key -- so it must not happen."""
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        return httpx.Response(500, json={"error": "boom"})

    client, _ = build_client(handler)
    with pytest.raises(ApiError):
        client.post("/order", json_body={"quantity": 1})
    assert calls["n"] == 1


def test_post_with_idempotency_key_is_retried_and_sends_the_header():
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        if len(calls) < 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    client, _ = build_client(handler)
    client.post("/order", json_body={"quantity": 1}, idempotency_key="abc-123")
    assert len(calls) == 2
    assert {c.headers["Idempotency-Key"] for c in calls} == {"abc-123"}


def test_timeouts_are_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ConnectTimeout("too slow", request=request)
        return httpx.Response(200, json={"ok": True})

    client, sink = build_client(handler)
    assert client.get("/thing") == {"ok": True}
    assert sink.records[0].error.startswith("ConnectTimeout")


def test_secrets_are_scrubbed_from_logged_bodies():
    scrubbed = scrub(
        {"client_secret": "hunter2", "nested": {"authorization": "Bearer x", "keep": 1}},
        ("client_secret", "authorization"),
    )
    assert scrubbed == {"client_secret": "***", "nested": {"authorization": "***", "keep": 1}}


def test_token_bucket_limits_rate():
    now = {"t": 0.0}
    slept: list[float] = []

    def clock():
        return now["t"]

    def sleep(seconds):
        slept.append(seconds)
        now["t"] += seconds

    bucket = TokenBucket(rate=2.0, capacity=2, clock=clock, sleep=sleep)
    bucket.acquire()
    bucket.acquire()
    assert slept == []
    bucket.acquire()  # bucket empty -> must wait ~0.5s for a refill
    assert slept and abs(slept[0] - 0.5) < 1e-6
