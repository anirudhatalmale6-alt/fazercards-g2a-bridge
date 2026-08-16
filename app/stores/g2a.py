"""G2A Import API adapter (api.g2a.com).

Auth is OAuth2 client_credentials: POST /oauth/token gives a bearer token valid
for an hour, which we cache and refresh a minute early rather than waiting for a
401.

Two things about this API shape the design of everything above it:

1. ``GET /v1/products`` has **no name search** -- only paging, 20 per page.  You
   cannot ask "is there a G2A product called X".  So we mirror the catalogue
   into ``store_products`` and match locally.  That is why the bridge needs a
   database rather than being a stateless script.
2. Writes are **asynchronous**.  addOffer and updateOffer return 202 with a
   jobId; the offer does not exist yet when the call returns.  We store the job
   id and reconcile it, instead of assuming success.
"""

from __future__ import annotations

import datetime as dt
import time
from decimal import Decimal
from typing import Any, Iterator

from app.config import Settings, get_settings
from app.http.client import ApiError, AuthError, BaseApiClient
from app.http.ratelimit import TokenBucket
from app.logging_conf import get_logger
from app.stores.base import JobResult, StoreCatalogItem, StoreOfferView, StoreOrderView

logger = get_logger(__name__)

PRODUCTS_PAGE_SIZE = 20  # fixed by the API, not a choice
OFFERS_PAGE_SIZE = 100
TOKEN_REFRESH_MARGIN_SECONDS = 60

# G2A's public documentation page lists operations by name but does not render
# the URL for every one of them, and the machine-readable api.yml sits behind
# their WAF.  The paths below follow the documented pattern and are collected
# here so that any correction needed after the first live call is a one-line
# change in one file, not a hunt through the adapter.
PATHS = {
    "token": "/oauth/token",
    "products": "/v1/products",
    "offers": "/v1/offers",
    "offer": "/v1/offers/{offer_id}",
    "product_offers": "/v1/products/{product_id}/offers",
    "job": "/v1/jobs/{job_id}",
    "seller_orders": "/v4/seller/orders",
    "seller_order": "/v4/seller/orders/{order_id}",
}


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _parse_dt(value: Any) -> dt.datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    for parser in (
        lambda t: dt.datetime.fromisoformat(t),
        lambda t: dt.datetime.strptime(t, "%Y-%m-%d %H:%M:%S"),
    ):
        try:
            parsed = parser(text)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
        except Exception:
            continue
    return None


class G2AClient(BaseApiClient):
    """Transport plus OAuth2 token handling."""

    def __init__(self, settings: Settings | None = None, **kw: Any):
        settings = settings or get_settings()
        super().__init__(
            service="g2a",
            base_url=settings.g2a_base_url,
            settings=settings,
            bucket=kw.pop(
                "bucket", TokenBucket(settings.g2a_rate_per_second, settings.g2a_burst)
            ),
            **kw,
        )
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # -------------------------------------------------------------- oauth2
    def _fetch_token(self) -> str:
        client_id, client_secret = self.settings.require_g2a_credentials()
        # Deliberately not via self.request(): that would recurse into
        # auth_headers() and we must not send a stale bearer to the token
        # endpoint.
        assert self.client is not None
        response = self.client.post(
            PATHS["token"],
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code != 200:
            raise AuthError(
                f"token request failed: {response.text[:300]}",
                service=self.service,
                status_code=response.status_code,
            )
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise AuthError("token response has no access_token", service=self.service)
        expires_in = float(payload.get("expires_in") or 3600)
        self._token = token
        self._token_expires_at = time.time() + max(
            30.0, expires_in - TOKEN_REFRESH_MARGIN_SECONDS
        )
        logger.info("g2a: obtained access token, valid for %.0fs", expires_in)
        return token

    def token(self) -> str:
        if self._token is None or time.time() >= self._token_expires_at:
            return self._fetch_token()
        return self._token

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}", "Accept": "application/json"}

    def on_auth_failure(self) -> None:
        """A 401 mid-flight means the token died early -- drop and re-fetch."""
        logger.info("g2a: access token rejected, refreshing")
        self._token = None
        self._token_expires_at = 0.0

    def decode_error(self, response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return response.text[:500] or response.reason_phrase
        if isinstance(payload, dict):
            errors = payload.get("errors")
            if isinstance(errors, list) and errors:
                parts = []
                for err in errors:
                    if isinstance(err, dict):
                        parts.append(
                            f"{err.get('code', '?')}: {err.get('message', err.get('detail', ''))}"
                        )
                    else:
                        parts.append(str(err))
                return "; ".join(parts)
        return super().decode_error(response)


class G2AAdapter:
    code = "g2a"

    def __init__(self, client: G2AClient | None = None, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.client = client or G2AClient(self.settings)

    # ------------------------------------------------------------- catalogue
    def iter_catalog(
        self,
        *,
        updated_since: dt.datetime | None = None,
        include_out_of_stock: bool = True,
        max_pages: int | None = None,
    ) -> Iterator[StoreCatalogItem]:
        """Page through the product catalogue.

        A full crawl is expensive (20 products per page), so callers should pass
        ``updated_since`` for routine refreshes and only omit it for the initial
        build.
        """
        page = 1
        seen = 0
        while True:
            params: dict[str, Any] = {
                "page": page,
                "includeOutOfStock": "true" if include_out_of_stock else "false",
            }
            if updated_since is not None:
                params["updatedAtFrom"] = updated_since.strftime("%Y-%m-%d %H:%M:%S")

            payload = self.client.get(PATHS["products"], params=params) or {}
            docs = payload.get("docs") or []
            if not docs:
                return
            for doc in docs:
                item = self._parse_catalog_item(doc)
                if item is not None:
                    yield item
            seen += len(docs)

            total = payload.get("total")
            if isinstance(total, int) and seen >= total:
                return
            if len(docs) < PRODUCTS_PAGE_SIZE:
                return
            page += 1
            if max_pages is not None and page > max_pages:
                logger.warning(
                    "g2a: stopped catalogue crawl at the %d page cap (%d products seen)",
                    max_pages, seen,
                )
                return

    @staticmethod
    def _parse_catalog_item(doc: dict) -> StoreCatalogItem | None:
        product_id = doc.get("id")
        name = doc.get("name")
        if not product_id or not name:
            return None
        limits = doc.get("priceLimit") or {}
        return StoreCatalogItem(
            product_ext_id=str(product_id),
            name=str(name),
            product_type=doc.get("type"),
            slug=doc.get("slug"),
            region=doc.get("region"),
            platform=doc.get("platform"),
            qty=int(doc.get("qty") or 0),
            min_price=_dec(doc.get("minPrice")),
            retail_min_price=_dec(doc.get("retail_min_price")),
            available_to_buy=bool(doc.get("availableToBuy", True)),
            price_limit_min=_dec(limits.get("min")),
            price_limit_max=_dec(limits.get("max")),
            remote_updated_at=_parse_dt(doc.get("updated_at")),
            raw=doc,
        )

    # ----------------------------------------------------------------- offers
    def list_offers(
        self, *, offer_type: str = "dropshipping", active: bool | None = None
    ) -> Iterator[StoreOfferView]:
        page = 1
        while True:
            params: dict[str, Any] = {
                "page": page,
                "itemsPerPage": OFFERS_PAGE_SIZE,
                "type[]": offer_type,
            }
            if active is not None:
                params["active"] = "true" if active else "false"
            payload = self.client.get(PATHS["offers"], params=params) or {}
            rows = payload.get("data") or []
            for row in rows:
                view = self._parse_offer(row)
                if view is not None:
                    yield view
            meta = payload.get("meta") or {}
            total = meta.get("totalResults")
            if not rows or len(rows) < OFFERS_PAGE_SIZE:
                return
            if isinstance(total, int) and page * OFFERS_PAGE_SIZE >= total:
                return
            page += 1

    @staticmethod
    def _parse_offer(row: dict) -> StoreOfferView | None:
        offer_id = row.get("id")
        product = row.get("product") or {}
        product_id = product.get("id")
        if not offer_id or not product_id:
            return None
        inventory = row.get("inventory") or {}
        return StoreOfferView(
            offer_ext_id=str(offer_id),
            product_ext_id=str(product_id),
            price=_dec(row.get("price")),
            inventory_size=int(inventory.get("size") or 0),
            status=str(row.get("status") or "unknown"),
            offer_type=row.get("type"),
            raw=row,
        )

    def offers_for_product(self, product_ext_id: str) -> list[StoreOfferView]:
        """Our offers on one product.

        This is how the bridge recovers the offer id when a create returned only
        a job id, or when a create came back 409 because the offer already
        existed.  Without it, an offer whose job we never saw would be created
        again and again.
        """
        try:
            payload = self.client.get(
                PATHS["product_offers"].format(product_id=product_ext_id)
            ) or {}
        except ApiError as exc:
            if exc.status_code == 404:
                return []
            raise
        rows = payload.get("data") or []
        if isinstance(rows, dict):
            rows = [rows]
        return [v for v in (self._parse_offer(r) for r in rows) if v is not None]

    def create_offer(
        self,
        *,
        product_ext_id: str,
        price: Decimal,
        inventory_size: int,
        active: bool = True,
        visibility: str = "all",
    ) -> JobResult:
        body = {
            "offerType": "dropshipping",
            "variants": [
                {
                    "price": {"retail": f"{price:.2f}"},
                    "productId": str(product_ext_id),
                    "active": active,
                    "inventory": {"size": int(inventory_size)},
                    "visibility": visibility,
                }
            ],
        }
        payload = self.client.post(PATHS["offers"], json_body=body, expected=(200, 201, 202)) or {}
        return self._parse_job(payload)

    def update_offer(
        self,
        offer_ext_id: str,
        *,
        price: Decimal | None = None,
        inventory_size: int | None = None,
        active: bool | None = None,
        archive: bool | None = None,
        visibility: str | None = None,
    ) -> JobResult:
        variant: dict[str, Any] = {}
        if price is not None:
            variant["price"] = {"retail": f"{price:.2f}"}
        if inventory_size is not None:
            variant["inventory"] = {"size": int(inventory_size)}
        if active is not None:
            variant["active"] = active
        if archive is not None:
            variant["archive"] = archive
        if visibility is not None:
            variant["visibility"] = visibility
        if not variant:
            raise ValueError("update_offer called with nothing to change")

        payload = self.client.request(
            "PATCH",
            PATHS["offer"].format(offer_id=offer_ext_id),
            json_body={"offerType": "dropshipping", "variant": variant},
            expected=(200, 202),
            # Safe to replay: it sets absolute values, it does not increment.
            allow_retry=True,
        ) or {}
        return self._parse_job(payload)

    def delete_offer(self, offer_ext_id: str) -> JobResult:
        """Note: G2A requires an offer to be archived before it can be deleted."""
        payload = self.client.delete(
            PATHS["offer"].format(offer_id=offer_ext_id), expected=(200, 202)
        ) or {}
        return self._parse_job(payload)

    def get_job(self, job_id: str) -> JobResult:
        payload = self.client.get(PATHS["job"].format(job_id=job_id)) or {}
        return self._parse_job(payload, job_id=job_id)

    @staticmethod
    def _parse_job(payload: dict, *, job_id: str | None = None) -> JobResult:
        data = payload.get("data") or {}
        elements = data.get("elements") or []
        errors = tuple(
            f"{el.get('code', 'error')}: {el.get('message', '')}".strip()
            for el in elements
            if isinstance(el, dict) and el.get("code")
        )
        resource_id = next(
            (
                el.get("resourceId")
                for el in elements
                if isinstance(el, dict) and el.get("resourceType") == "offer" and el.get("resourceId")
            ),
            None,
        )
        return JobResult(
            job_id=str(data.get("jobId") or job_id or ""),
            status=str(data.get("status") or "pending"),
            resource_id=resource_id,
            errors=errors,
            raw=payload,
        )

    def wait_for_job(
        self, job_id: str, *, timeout_seconds: float = 60.0, poll_seconds: float = 2.0
    ) -> JobResult:
        """Poll a job to completion.

        Used by the CLI and tests.  The sync workers do not block on this -- they
        record the job id and reconcile it on the next pass, so one slow job
        cannot stall a run of thousands of products.
        """
        deadline = time.monotonic() + timeout_seconds
        result = JobResult(job_id=job_id)
        while time.monotonic() < deadline:
            result = self.get_job(job_id)
            if result.is_finished:
                return result
            time.sleep(poll_seconds)
        return result

    # ----------------------------------------------------------------- orders
    def list_orders(
        self,
        *,
        statuses: tuple[str, ...] = ("completed", "delivery"),
        created_from: dt.datetime | None = None,
        page_size: int = 100,
        max_pages: int = 50,
    ) -> Iterator[StoreOrderView]:
        page = 1
        while page <= max_pages:
            params: dict[str, Any] = {
                "page": page,
                "itemsPerPage": page_size,
                "sortBy": "purchase_date",
                "sortStrategy": "DESC",
            }
            if statuses:
                params["statuses"] = list(statuses)
            if created_from is not None:
                params["createdAt[from]"] = created_from.strftime("%Y-%m-%dT%H:%M:%SZ")

            payload = self.client.get(PATHS["seller_orders"], params=params) or {}
            rows = payload.get("data") or []
            for row in rows:
                view = self._parse_order(row)
                if view is not None:
                    yield view
            meta = payload.get("meta") or {}
            if not meta.get("hasNext") or not rows:
                return
            page += 1

    def get_order(self, order_ext_id: str) -> StoreOrderView | None:
        try:
            payload = self.client.get(
                PATHS["seller_order"].format(order_id=order_ext_id)
            ) or {}
        except ApiError as exc:
            if exc.status_code == 404:
                return None
            raise
        return self._parse_order(payload.get("data") or {})

    @staticmethod
    def _parse_order(row: dict) -> StoreOrderView | None:
        order_id = row.get("id")
        if not order_id:
            return None
        # The documented sample shows `items` as a single object even though the
        # field reads like a list, so normalise both shapes.
        raw_items = row.get("items")
        if isinstance(raw_items, dict):
            items: tuple[dict, ...] = (raw_items,)
        elif isinstance(raw_items, list):
            items = tuple(i for i in raw_items if isinstance(i, dict))
        else:
            items = ()
        return StoreOrderView(
            order_ext_id=str(order_id),
            status=str(row.get("status") or "unknown"),
            items=items,
            raw=row,
        )
