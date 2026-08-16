"""FazerCards adapter (https://api.fzr.cards, /api/v2).

Auth is ``X-API-Key: fc_...``.  Responses wrap payloads in ``{ok: true, ...}``
and report failures as ``{ok: false, error, code}`` -- including some that come
back with a 200, which is why ``_unwrap`` checks ``ok`` rather than trusting the
status code.

Catalogue shape (two families, same structure):

  gift cards: GET /giftcards        -> categories {category_id, name}
              GET /giftcards/cards  -> offers[]  {card_id, price_usd, stock, ...}
  game keys:  GET /gamekeys         -> games {game_id, name, region, platform}
              GET /gamekeys/keys    -> keys[]   {key_id, price_usd, stock, ...}

Ordering needs *both* ids because the offer id is only unique inside its
category.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Iterator

from app.config import Settings, get_settings
from app.http.client import ApiError, BaseApiClient
from app.http.ratelimit import TokenBucket
from app.logging_conf import get_logger
from app.models import ProductKind
from app.suppliers.base import (
    InsufficientBalance,
    OutOfStock,
    PurchasedCode,
    PurchaseResult,
    SupplierItem,
    SupplierUnavailable,
)

logger = get_logger(__name__)

API_PREFIX = "/api/v2"
PAGE_SIZE = 200

# Error codes that mean "do not retry, the situation has to change first".
_TERMINAL_CODES = {
    "out_of_stock": OutOfStock,
    "insufficient_stock": OutOfStock,
    "insufficient_balance": InsufficientBalance,
    "insufficient_funds": InsufficientBalance,
    "subscription_inactive": SupplierUnavailable,
}


def _to_decimal(value: Any) -> Decimal:
    """FazerCards sends prices as strings; be strict about what we accept."""
    if value is None:
        raise ValueError("missing price")
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"unparseable price {value!r}") from exc
    if price < 0:
        raise ValueError(f"negative price {value!r}")
    return price


class FazerCardsClient(BaseApiClient):
    def __init__(self, settings: Settings | None = None, **kw: Any):
        settings = settings or get_settings()
        super().__init__(
            service="fazercards",
            base_url=settings.fazercards_base_url,
            settings=settings,
            bucket=kw.pop(
                "bucket",
                TokenBucket(settings.fazercards_rate_per_second, settings.fazercards_burst),
            ),
            **kw,
        )

    def auth_headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self.settings.require_fazercards_key(),
            "Accept": "application/json",
        }

    def decode_error(self, response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return response.text[:500] or response.reason_phrase
        if isinstance(payload, dict) and "error" in payload:
            code = payload.get("code")
            return f"{payload['error']}" + (f" [{code}]" if code else "")
        return super().decode_error(response)


class FazerCardsAdapter:
    """Supplier adapter over the FazerCards client."""

    code = "fazercards"

    def __init__(self, client: FazerCardsClient | None = None, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.client = client or FazerCardsClient(self.settings)

    # ------------------------------------------------------------- internals
    def _unwrap(self, payload: Any, *, context: str) -> dict:
        """Return the body, or raise -- including for a 200 that says ok:false."""
        if not isinstance(payload, dict):
            raise ApiError(
                f"{context}: expected a JSON object, got {type(payload).__name__}",
                service=self.code,
            )
        if payload.get("ok") is False:
            code = str(payload.get("code") or "").lower()
            message = payload.get("error") or "unknown error"
            exc_cls = _TERMINAL_CODES.get(code)
            if exc_cls is not None:
                raise exc_cls(f"{context}: {message}")
            raise ApiError(f"{context}: {message}", service=self.code, payload=payload)
        return payload

    def _paged(self, path: str, key: str) -> Iterator[dict]:
        """Walk a cursor-paginated list endpoint.

        The cursor is opaque; we stop on ``has_more == false`` or a null cursor,
        and guard against a server that keeps handing back the same cursor.
        """
        cursor: str | None = None
        seen_cursors: set[str] = set()
        pages = 0
        while True:
            params: dict[str, Any] = {"limit": PAGE_SIZE}
            if cursor:
                params["cursor"] = cursor
            body = self._unwrap(
                self.client.get(f"{API_PREFIX}{path}", params=params),
                context=f"GET {path}",
            )
            items = body.get(key) or []
            for item in items:
                yield item
            pages += 1
            meta = body.get("meta") or {}
            next_cursor = meta.get("next_cursor")
            if not meta.get("has_more") or not next_cursor:
                return
            if next_cursor in seen_cursors:
                logger.error(
                    "%s: cursor %r repeated after %d pages -- stopping to avoid a loop",
                    path, next_cursor, pages,
                )
                return
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    # ------------------------------------------------------------- catalogue
    def iter_gift_cards(self) -> Iterator[SupplierItem]:
        for category in self._paged("/giftcards", "items"):
            category_id = category.get("category_id")
            if not category_id:
                continue
            body = self._unwrap(
                self.client.get(
                    f"{API_PREFIX}/giftcards/cards", params={"category_id": category_id}
                ),
                context=f"GET /giftcards/cards?category_id={category_id}",
            )
            category_name = body.get("name") or category.get("name") or ""
            for offer in body.get("offers") or []:
                card_id = offer.get("card_id")
                if not card_id:
                    # A denomination with no id cannot be ordered; skipping it is
                    # the only safe option.
                    logger.debug("skipping id-less gift card offer in %s", category_id)
                    continue
                try:
                    price = _to_decimal(offer.get("price_usd"))
                except ValueError as exc:
                    logger.warning("skipping %s/%s: %s", category_id, card_id, exc)
                    continue
                yield SupplierItem(
                    kind=ProductKind.GIFT_CARD,
                    category_ext_id=str(category_id),
                    offer_ext_id=str(card_id),
                    category_name=category_name,
                    name=self._compose_name(category_name, offer.get("name")),
                    price=price,
                    stock=int(offer.get("stock") or 0),
                    min_order_quantity=int(offer.get("min_order_quantity") or 1),
                    max_order_quantity=int(offer.get("max_order_quantity") or 1),
                    raw={"category": category, "offer": offer},
                )

    def iter_game_keys(self) -> Iterator[SupplierItem]:
        for game in self._paged("/gamekeys", "items"):
            game_id = game.get("game_id")
            if not game_id:
                continue
            body = self._unwrap(
                self.client.get(f"{API_PREFIX}/gamekeys/keys", params={"game_id": game_id}),
                context=f"GET /gamekeys/keys?game_id={game_id}",
            )
            game_name = body.get("GameName") or game.get("name") or ""
            region = body.get("region") or game.get("region")
            platform = body.get("platform") or game.get("platform")
            restricted = bool(
                body.get("region_restriction") or game.get("region_restriction")
            )
            for offer in body.get("keys") or []:
                key_id = offer.get("key_id")
                if not key_id:
                    logger.debug("skipping id-less key offer in %s", game_id)
                    continue
                try:
                    price = _to_decimal(offer.get("price_usd"))
                except ValueError as exc:
                    logger.warning("skipping %s/%s: %s", game_id, key_id, exc)
                    continue
                yield SupplierItem(
                    kind=ProductKind.GAME_KEY,
                    category_ext_id=str(game_id),
                    offer_ext_id=str(key_id),
                    category_name=game_name,
                    name=self._compose_name(game_name, offer.get("name")),
                    price=price,
                    stock=int(offer.get("stock") or 0),
                    region=region,
                    platform=platform,
                    region_restricted=restricted,
                    min_order_quantity=int(offer.get("min_order_quantity") or 1),
                    max_order_quantity=int(offer.get("max_order_quantity") or 1),
                    raw={"game": game, "offer": offer},
                )

    @staticmethod
    def _compose_name(category_name: str | None, offer_name: str | None) -> str:
        """Offer names are often just the denomination ("50 USD").

        Prefixing the category keeps the title meaningful for matching, while
        avoiding "Roblox - Roblox Card 350 HKD" when the offer already repeats it.
        """
        category = (category_name or "").strip()
        offer = (offer_name or "").strip()
        if not offer:
            return category
        if not category:
            return offer
        if category.lower() in offer.lower():
            return offer
        # The category often already ends with the offer name ("Xbox Game Pass
        # Essential 6 Months" + "6 Months").  Concatenating would produce
        # "... 6 Months 6 Months", which measurably hurts title matching.
        if offer.lower() in category.lower():
            return category
        return f"{category} {offer}"

    def iter_products(self) -> Iterable[SupplierItem]:
        yield from self.iter_game_keys()
        yield from self.iter_gift_cards()

    # -------------------------------------------------------------- ordering
    def purchase(
        self, item: SupplierItem, quantity: int, *, idempotency_key: str
    ) -> PurchaseResult:
        if quantity < item.min_order_quantity or (
            item.max_order_quantity and quantity > item.max_order_quantity
        ):
            raise ValueError(
                f"quantity {quantity} outside supplier limits "
                f"[{item.min_order_quantity}, {item.max_order_quantity}]"
            )

        if item.kind is ProductKind.GAME_KEY:
            path, body = f"{API_PREFIX}/gamekeys/order", {
                "game_id": item.category_ext_id,
                "key_id": item.offer_ext_id,
                "quantity": quantity,
            }
        else:
            path, body = f"{API_PREFIX}/giftcards/order", {
                "category_id": item.category_ext_id,
                "card_id": item.offer_ext_id,
                "quantity": quantity,
            }

        payload = self._unwrap(
            self.client.post(
                path,
                json_body=body,
                idempotency_key=idempotency_key,
                expected=(200, 201, 202),
            ),
            context=f"POST {path}",
        )
        return self._parse_order(payload.get("order") or {})

    def get_order(self, order_ext_id: str) -> PurchaseResult | None:
        try:
            payload = self._unwrap(
                self.client.get(f"{API_PREFIX}/orders/{order_ext_id}"),
                context=f"GET /orders/{order_ext_id}",
            )
        except ApiError as exc:
            if exc.status_code == 404:
                return None
            raise
        return self._parse_order(payload.get("order") or {})

    def balance(self) -> Decimal | None:
        payload = self._unwrap(
            self.client.get(f"{API_PREFIX}/balance"), context="GET /balance"
        )
        raw = payload.get("balance")
        return None if raw is None else _to_decimal(raw)

    # ---------------------------------------------------------------- parsing
    @staticmethod
    def _extract_codes(order: dict) -> tuple[PurchasedCode, ...]:
        """Pull the codes out of an order.

        The order object is documented as free-form, and the delivered codes
        appear under different names per product family, so we look in all of
        them and accept both bare strings and objects.
        """
        codes: list[PurchasedCode] = []
        for field_name in ("cards", "keys", "codes", "items"):
            for entry in order.get(field_name) or []:
                if isinstance(entry, str):
                    value = entry.strip()
                    if value:
                        codes.append(PurchasedCode(value=value))
                elif isinstance(entry, dict):
                    for key in ("code", "key", "value", "card", "serial"):
                        raw = entry.get(key)
                        if isinstance(raw, str) and raw.strip():
                            pin = entry.get("pin")
                            value = f"{raw.strip()} / PIN: {pin}" if pin else raw.strip()
                            codes.append(PurchasedCode(value=value))
                            break
            if codes:
                break
        return tuple(codes)

    def _parse_order(self, order: dict) -> PurchaseResult:
        order_id = order.get("id") or order.get("order_id") or order.get("orderId")
        if not order_id:
            raise ApiError(
                "supplier order response has no id -- cannot track this purchase",
                service=self.code,
                payload=order,
            )
        cost = order.get("total_usd") or order.get("total") or order.get("amount")
        return PurchaseResult(
            supplier_order_ext_id=str(order_id),
            codes=self._extract_codes(order),
            cost=_to_decimal(cost) if cost is not None else None,
            status=str(order.get("status") or "completed"),
            raw=order,
        )
