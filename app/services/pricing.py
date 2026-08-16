"""Turn a supplier cost into a store price.

    price = supplier_price(USD) x FX(USD->EUR) x (1 + markup%)  -> rounded -> clamped

Three safety rules are baked in, each of which exists because the alternative
loses money:

* **A stale FX rate is refused, not used.**  If the FX provider has been down
  longer than ``fx_max_age_hours`` we raise instead of pricing off last week's
  rate.  A pricing job that stops is recoverable; a catalogue published 8% under
  cost is not.
* **A floor price.**  Rounding and markup maths on a $0.02 item can produce
  something absurd; ``min_offer_price`` is the backstop.
* **The store's own price limits win.**  If the store publishes a min/max for a
  product we clamp to it, because a rejected offer helps nobody.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.logging_conf import get_logger
from app.models import FxRate, ProductMapping, StoreProduct, SupplierProduct

logger = get_logger(__name__)


class PricingError(RuntimeError):
    pass


class StaleFxRate(PricingError):
    pass


@dataclass(frozen=True)
class PriceBreakdown:
    """Every input to a published price, so any price can be explained later."""

    supplier_price: Decimal
    fx_rate: Decimal
    markup_percent: Decimal
    raw_price: Decimal
    final_price: Decimal
    clamped: bool = False
    floored: bool = False
    reason: str = ""


class FxProvider:
    """Fetches and caches the USD->EUR rate."""

    def __init__(self, session: Session, *, settings: Settings | None = None, client: httpx.Client | None = None):
        self.session = session
        self.settings = settings or get_settings()
        self.client = client

    def _cached(self) -> FxRate | None:
        return self.session.execute(
            select(FxRate)
            .where(
                FxRate.base == self.settings.supplier_currency,
                FxRate.quote == self.settings.store_currency,
            )
            .order_by(FxRate.fetched_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    def _fetch(self) -> Decimal:
        client = self.client or httpx.Client(timeout=15.0)
        try:
            response = client.get(self.settings.fx_provider_url)
            response.raise_for_status()
            payload = response.json()
        finally:
            if self.client is None:
                client.close()
        rates = payload.get("rates") or {}
        raw = rates.get(self.settings.store_currency)
        if raw is None:
            raise PricingError(
                f"FX provider returned no {self.settings.store_currency} rate: {payload}"
            )
        rate = Decimal(str(raw))
        if rate <= 0:
            raise PricingError(f"FX provider returned a non-positive rate: {rate}")
        return rate

    def rate(self, *, force_refresh: bool = False) -> Decimal:
        # An explicitly pinned rate wins -- useful when the client wants a fixed
        # internal rate rather than a floating one.
        if self.settings.fx_rate_usd_eur is not None:
            return Decimal(str(self.settings.fx_rate_usd_eur))

        cached = None if force_refresh else self._cached()
        if cached is not None:
            fetched_at = cached.fetched_at
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=dt.timezone.utc)
            age = dt.datetime.now(dt.timezone.utc) - fetched_at
            if age < dt.timedelta(hours=self.settings.fx_max_age_hours):
                return Decimal(str(cached.rate))

        try:
            rate = self._fetch()
        except Exception as exc:
            if cached is not None:
                raise StaleFxRate(
                    f"FX refresh failed ({exc}) and the cached rate is older than "
                    f"{self.settings.fx_max_age_hours}h -- refusing to price off it"
                ) from exc
            raise PricingError(f"no FX rate available: {exc}") from exc

        self.session.add(
            FxRate(
                base=self.settings.supplier_currency,
                quote=self.settings.store_currency,
                rate=rate,
                source=self.settings.fx_provider_url,
            )
        )
        self.session.flush()
        logger.info(
            "fx: 1 %s = %s %s", self.settings.supplier_currency, rate, self.settings.store_currency
        )
        return rate


class Pricer:
    def __init__(self, session: Session, *, settings: Settings | None = None, fx: FxProvider | None = None):
        self.session = session
        self.settings = settings or get_settings()
        self.fx = fx or FxProvider(session, settings=self.settings)

    def _round(self, value: Decimal) -> Decimal:
        mode = self.settings.price_rounding
        if mode == "0.99":
            # ".99 pricing": round up to the next whole unit, minus a cent.
            whole = value.to_integral_value(rounding=ROUND_HALF_UP)
            if whole < value:
                whole += 1
            return max(Decimal("0.99"), whole - Decimal("0.01"))
        step = Decimal(mode)
        return (value / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step

    def compute(
        self,
        supplier_product: SupplierProduct,
        mapping: ProductMapping | None = None,
        store_product: StoreProduct | None = None,
    ) -> PriceBreakdown:
        supplier_price = Decimal(str(supplier_product.price_supplier))

        if mapping is not None and mapping.fixed_price_override is not None:
            fixed = Decimal(str(mapping.fixed_price_override))
            return PriceBreakdown(
                supplier_price=supplier_price,
                fx_rate=Decimal("1"),
                markup_percent=Decimal("0"),
                raw_price=fixed,
                final_price=self._round(fixed),
                reason="fixed price override",
            )

        rate = self.fx.rate()
        markup = Decimal(
            str(
                mapping.markup_percent_override
                if mapping is not None and mapping.markup_percent_override is not None
                else self.settings.default_markup_percent
            )
        )
        raw = supplier_price * rate * (Decimal("1") + markup / Decimal("100"))
        final = self._round(raw)

        floored = False
        floor = Decimal(str(self.settings.min_offer_price))
        if final < floor:
            final = floor
            floored = True

        clamped = False
        reason = ""
        if store_product is not None:
            if store_product.price_limit_min:
                limit = Decimal(str(store_product.price_limit_min))
                if limit > 0 and final < limit:
                    final, clamped = limit, True
                    reason = f"raised to store minimum {limit}"
            if store_product.price_limit_max:
                limit = Decimal(str(store_product.price_limit_max))
                if limit > 0 and final > limit:
                    final, clamped = limit, True
                    reason = f"lowered to store maximum {limit}"

        return PriceBreakdown(
            supplier_price=supplier_price,
            fx_rate=rate,
            markup_percent=markup,
            raw_price=raw.quantize(Decimal("0.0001")),
            final_price=final.quantize(Decimal("0.01")),
            clamped=clamped,
            floored=floored,
            reason=reason,
        )
