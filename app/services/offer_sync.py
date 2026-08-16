"""Push mapped products to the store as offers, and keep them current.

The whole job is "make the store agree with the database, cheaply".  Three rules
do most of the work:

1. **Only push differences.**  Every offer remembers what we last sent; if the
   newly computed price, stock and active flag are identical we make no call at
   all.  On a 3,000-product catalogue refreshed every 12 minutes, that is the
   difference between ~15,000 calls/hour and a few dozen.
2. **Price changes are budgeted.**  The store caps price changes per product per
   hour.  When the budget is spent we push the stock change and defer the price
   to the next run, rather than sending a call we know will be rejected.
3. **Out of stock deactivates, it never deletes.**  A supplier that reports 0
   for an hour must not cost us the offer and its history -- we set inventory 0
   and active false, and switch it straight back on when stock returns.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.http.client import ApiError, RateLimitedError
from app.http.ratelimit import PriceChangeBudget
from app.logging_conf import get_logger
from app.models import (
    MappingStatus,
    OfferState,
    ProductMapping,
    StoreOffer,
    StoreProduct,
    SupplierProduct,
)
from app.services.catalog_sync import SyncRunRecorder
from app.services.pricing import Pricer, PricingError

logger = get_logger(__name__)

SYNCABLE_STATUSES = (MappingStatus.AUTO.value, MappingStatus.APPROVED.value)


@dataclass
class DesiredOffer:
    price: Decimal
    inventory: int
    active: bool


class OfferSync:
    def __init__(
        self,
        session: Session,
        adapter,
        *,
        settings: Settings | None = None,
        pricer: Pricer | None = None,
    ):
        self.session = session
        self.adapter = adapter
        self.settings = settings or get_settings()
        self.pricer = pricer or Pricer(session, settings=self.settings)
        self.budget = PriceChangeBudget(
            session,
            store_code=adapter.code,
            limit=self.settings.effective_price_change_budget,
        )

    # ------------------------------------------------------------- selection
    def _syncable(self, limit: int | None = None):
        stmt = (
            select(ProductMapping, SupplierProduct, StoreProduct)
            .join(SupplierProduct, ProductMapping.supplier_product_id == SupplierProduct.id)
            .join(StoreProduct, ProductMapping.store_product_id == StoreProduct.id)
            .where(
                ProductMapping.store_code == self.adapter.code,
                ProductMapping.status.in_(SYNCABLE_STATUSES),
                ProductMapping.sync_enabled.is_(True),
                SupplierProduct.is_active.is_(True),
            )
        )
        if limit:
            stmt = stmt.limit(limit)
        return self.session.execute(stmt).all()

    def _desired(
        self, mapping: ProductMapping, supplier_product: SupplierProduct, store_product: StoreProduct
    ) -> DesiredOffer:
        breakdown = self.pricer.compute(supplier_product, mapping, store_product)
        stock = int(supplier_product.stock or 0)
        # Never advertise more than the supplier will actually sell us in one go.
        if supplier_product.max_order_quantity:
            stock = min(stock, int(supplier_product.max_order_quantity) * 50)
        return DesiredOffer(
            price=breakdown.final_price, inventory=stock, active=stock > 0
        )

    def _offer_row(self, mapping: ProductMapping, store_product: StoreProduct) -> StoreOffer:
        offer = mapping.offer
        if offer is None:
            offer = StoreOffer(
                mapping_id=mapping.id,
                store_code=self.adapter.code,
                product_ext_id=store_product.product_ext_id,
            )
            self.session.add(offer)
            self.session.flush()
        return offer

    # ------------------------------------------------------------------ push
    def run(self, *, limit: int | None = None, dry_run: bool = False) -> dict[str, int]:
        now = dt.datetime.now(dt.timezone.utc)

        with SyncRunRecorder(self.session, f"offers:{self.adapter.code}") as run:
            for mapping, supplier_product, store_product in self._syncable(limit):
                run.bump("examined")
                try:
                    desired = self._desired(mapping, supplier_product, store_product)
                except PricingError as exc:
                    # Pricing is a whole-run problem (bad FX), not a per-item one.
                    run.bump("errors")
                    logger.error("pricing failed, aborting run: %s", exc)
                    raise

                offer = self._offer_row(mapping, store_product)
                action = self._sync_one(offer, desired, dry_run=dry_run, now=now)
                run.bump(action)
            self.session.flush()
            return dict(run.stats)

    def _sync_one(
        self, offer: StoreOffer, desired: DesiredOffer, *, dry_run: bool, now: dt.datetime
    ) -> str:
        price_changed = (
            offer.pushed_price is None
            or Decimal(str(offer.pushed_price)) != desired.price
        )
        stock_changed = offer.pushed_inventory != desired.inventory
        active_changed = offer.pushed_active != desired.active

        if not (price_changed or stock_changed or active_changed):
            return "skipped_unchanged"

        if dry_run:
            return "would_create" if offer.offer_ext_id is None else "would_update"

        try:
            if offer.offer_ext_id is None and offer.pending_job_id:
                # A create is already in flight.  Creating again would either
                # duplicate the offer or collect a 409, so resolve the id first
                # and skip this round if the store has not finished yet.
                if not self._resolve_offer_id(offer):
                    return "awaiting_job"

            if offer.offer_ext_id is None:
                return self._create(offer, desired, now)
            return self._update(
                offer, desired, now,
                price_changed=price_changed,
                stock_changed=stock_changed,
                active_changed=active_changed,
            )
        except RateLimitedError as exc:
            # Rate limited after full back-off: leave the offer untouched so the
            # next run retries it, and do not mark it failed.
            offer.last_error = str(exc)
            self.session.flush()
            logger.warning("offer %s rate limited, deferring", offer.product_ext_id)
            return "deferred_rate_limited"
        except ApiError as exc:
            offer.last_error = str(exc)[:2000]
            offer.consecutive_failures += 1
            if offer.consecutive_failures >= 5:
                offer.state = OfferState.FAILED.value
            self.session.flush()
            logger.error("offer %s failed: %s", offer.product_ext_id, exc)
            return "errors"

    def _resolve_offer_id(self, offer: StoreOffer) -> bool:
        """Find the id of an offer we created but never got an id back for.

        Tries the job first, then asks the store which offers exist on that
        product.  Returns True when the id is now known.
        """
        if offer.pending_job_id:
            try:
                job = self.adapter.get_job(offer.pending_job_id)
            except ApiError as exc:
                logger.warning("job %s lookup failed: %s", offer.pending_job_id, exc)
                return False
            if job.is_finished:
                offer.pending_job_id = None
                if job.succeeded and job.resource_id:
                    offer.offer_ext_id = job.resource_id
                    self.session.flush()
                    return True
                if not job.succeeded:
                    offer.last_error = "; ".join(job.errors) or f"job {job.status}"
                    offer.state = OfferState.FAILED.value
                    offer.pushed_price = None
                    offer.pushed_inventory = None
                    offer.pushed_active = None
                    self.session.flush()
                    return False
            else:
                return False

        remote = self.adapter.offers_for_product(offer.product_ext_id)
        if remote:
            offer.offer_ext_id = remote[0].offer_ext_id
            self.session.flush()
            return True
        return False

    def _create(self, offer: StoreOffer, desired: DesiredOffer, now: dt.datetime) -> str:
        try:
            job = self.adapter.create_offer(
                product_ext_id=offer.product_ext_id,
                price=desired.price,
                inventory_size=desired.inventory,
                active=desired.active,
            )
        except ApiError as exc:
            if exc.status_code == 409:
                # The store already has an offer on this product -- adopt it
                # rather than failing forever.  This is the normal path when
                # taking over an account that was populated by something else.
                offer.pending_job_id = None
                if self._resolve_offer_id(offer):
                    logger.info("adopted existing offer for %s", offer.product_ext_id)
                    return self._update(
                        offer, desired, now,
                        price_changed=True, stock_changed=True, active_changed=True,
                    )
            raise

        offer.pending_job_id = job.job_id or None
        if job.resource_id:
            offer.offer_ext_id = job.resource_id
        elif offer.pending_job_id:
            # The store returns 202 with only a job id, so the offer id has to be
            # collected separately before the next run can update this offer.
            if (
                not self._resolve_offer_id(offer)
                and offer.state == OfferState.FAILED.value
            ):
                # The store rejected the offer.  Do not record it as pushed.
                return "errors"
        offer.state = (
            OfferState.LIVE.value if desired.active else OfferState.OUT_OF_STOCK.value
        )
        self._remember(offer, desired, now)
        self.budget.record(
            offer.product_ext_id, new_price=float(desired.price), offer_ext_id=offer.offer_ext_id
        )
        return "created"

    def _update(
        self,
        offer: StoreOffer,
        desired: DesiredOffer,
        now: dt.datetime,
        *,
        price_changed: bool,
        stock_changed: bool,
        active_changed: bool,
    ) -> str:
        send_price = price_changed
        deferred_price = False
        if price_changed and not self.budget.allows(offer.product_ext_id):
            # Out of price-change budget for this hour.  Push the rest now; the
            # price catches up on a later run.
            send_price = False
            deferred_price = True
            logger.info(
                "offer %s: price change deferred, hourly budget spent",
                offer.product_ext_id,
            )

        if not (send_price or stock_changed or active_changed):
            return "deferred_price_budget"

        job = self.adapter.update_offer(
            offer.offer_ext_id,
            price=desired.price if send_price else None,
            inventory_size=desired.inventory if stock_changed else None,
            active=desired.active if active_changed else None,
        )
        offer.pending_job_id = job.job_id or None
        offer.state = (
            OfferState.LIVE.value if desired.active else OfferState.OUT_OF_STOCK.value
        )

        old_price = offer.pushed_price
        self._remember(
            offer,
            desired,
            now,
            keep_price=not send_price,
        )
        if send_price:
            self.budget.record(
                offer.product_ext_id,
                new_price=float(desired.price),
                old_price=float(old_price) if old_price is not None else None,
                offer_ext_id=offer.offer_ext_id,
            )
        return "deferred_price_budget" if deferred_price else "updated"

    def _remember(
        self,
        offer: StoreOffer,
        desired: DesiredOffer,
        now: dt.datetime,
        *,
        keep_price: bool = False,
    ) -> None:
        """Record what the store now holds.

        ``keep_price`` matters: when a price change was deferred we must NOT
        record the new price, or the next run would believe it was already sent
        and the offer would sit at the old price forever.
        """
        if not keep_price:
            offer.pushed_price = desired.price
        offer.pushed_inventory = desired.inventory
        offer.pushed_active = desired.active
        offer.active = desired.active
        offer.last_pushed_at = now
        offer.last_error = None
        offer.consecutive_failures = 0
        self.session.flush()

    # ----------------------------------------------------------- reconcile
    def reconcile_jobs(self, *, limit: int = 200) -> dict[str, int]:
        """Resolve the async jobs left behind by create/update calls.

        Without this an offer that the store rejected (bad price, product not
        allowed for dropshipping) would look successful in our database forever.
        """
        stats = {"checked": 0, "completed": 0, "failed": 0, "pending": 0}
        rows = (
            self.session.execute(
                select(StoreOffer)
                .where(
                    StoreOffer.store_code == self.adapter.code,
                    StoreOffer.pending_job_id.is_not(None),
                )
                .limit(limit)
            )
            .scalars()
            .all()
        )
        for offer in rows:
            stats["checked"] += 1
            try:
                job = self.adapter.get_job(offer.pending_job_id)
            except ApiError as exc:
                logger.warning("job %s lookup failed: %s", offer.pending_job_id, exc)
                continue
            if not job.is_finished:
                stats["pending"] += 1
                continue
            offer.pending_job_id = None
            if job.succeeded:
                if job.resource_id and not offer.offer_ext_id:
                    offer.offer_ext_id = job.resource_id
                offer.last_error = None
                stats["completed"] += 1
            else:
                offer.last_error = "; ".join(job.errors) or f"job {job.status}"
                offer.consecutive_failures += 1
                offer.state = OfferState.FAILED.value
                # The push did not land, so forget what we thought we sent.
                offer.pushed_price = None
                offer.pushed_inventory = None
                offer.pushed_active = None
                stats["failed"] += 1
        self.session.flush()
        return stats

    def adopt_existing_offers(self) -> dict[str, int]:
        """Attach offers that already exist on the store to our mappings.

        Run once when taking over an account that was populated by something
        else -- without it the first sync would try to create offers the store
        already has and collect 409s.
        """
        stats = {"seen": 0, "adopted": 0, "unknown": 0}
        by_product = {
            product_ext_id: offer
            for product_ext_id, offer in (
                (o.product_ext_id, o) for o in self.adapter.list_offers()
            )
        }
        stats["seen"] = len(by_product)

        for mapping, _supplier_product, store_product in self._syncable():
            remote = by_product.get(store_product.product_ext_id)
            if remote is None:
                continue
            offer = self._offer_row(mapping, store_product)
            if offer.offer_ext_id == remote.offer_ext_id:
                continue
            offer.offer_ext_id = remote.offer_ext_id
            offer.pushed_price = remote.price
            offer.pushed_inventory = remote.inventory_size
            offer.pushed_active = remote.is_active
            offer.active = remote.is_active
            offer.state = (
                OfferState.LIVE.value if remote.is_active else OfferState.OUT_OF_STOCK.value
            )
            stats["adopted"] += 1

        mapped_products = {
            sp.product_ext_id
            for _m, _s, sp in self._syncable()
        }
        stats["unknown"] = len(set(by_product) - mapped_products)
        self.session.flush()
        return stats
