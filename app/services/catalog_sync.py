"""Pull catalogues into the local database.

Both directions follow the same rule: **upsert on the natural key, never insert
blindly**.  Re-running a sync must converge on the same rows, not multiply them.

The other rule worth stating: a product that disappears from a feed is *marked*
missing, and only deactivated after it has been missing for several consecutive
runs.  Supplier APIs return short pages and empty responses more often than they
admit, and one bad response must not deactivate a whole catalogue.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.logging_conf import get_logger
from app.models import (
    ProductKind,
    RunState,
    StoreProduct,
    Supplier,
    SupplierProduct,
    SyncRun,
)
from app.services.text import parse_title
from app.stores.base import StoreCatalogItem
from app.suppliers.base import SupplierItem

logger = get_logger(__name__)


def get_or_create_supplier(session: Session, code: str, name: str | None = None) -> Supplier:
    supplier = session.execute(
        select(Supplier).where(Supplier.code == code)
    ).scalar_one_or_none()
    if supplier is None:
        supplier = Supplier(code=code, name=name or code.title())
        session.add(supplier)
        session.flush()
    return supplier


class SyncRunRecorder:
    """Context manager that always closes out its sync_runs row.

    Even a crash leaves a row saying what failed and when, which is the
    difference between "the sync is broken" and "the sync broke at 04:12 on
    page 7 with a 502".
    """

    def __init__(self, session: Session, kind: str):
        self.session = session
        self.run = SyncRun(kind=kind, state=RunState.RUNNING.value, stats={})
        session.add(self.run)
        session.flush()
        self.stats: dict[str, int] = {}

    def __enter__(self) -> "SyncRunRecorder":
        return self

    def bump(self, key: str, amount: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + amount

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.run.stats = dict(self.stats)
        self.run.finished_at = dt.datetime.now(dt.timezone.utc)
        if exc is not None:
            self.run.state = RunState.FAILED.value
            self.run.error = f"{exc_type.__name__}: {exc}"[:4000]
        elif self.stats.get("errors"):
            self.run.state = RunState.PARTIAL.value
        else:
            self.run.state = RunState.SUCCESS.value
        self.session.flush()
        return False  # never swallow the exception


class SupplierCatalogSync:
    """FazerCards (or any supplier) -> supplier_products."""

    def __init__(self, session: Session, adapter, *, settings: Settings | None = None):
        self.session = session
        self.adapter = adapter
        self.settings = settings or get_settings()

    def run(self, items=None) -> dict[str, int]:
        supplier = get_or_create_supplier(self.session, self.adapter.code)
        now = dt.datetime.now(dt.timezone.utc)

        existing: dict[tuple[str, str, str], SupplierProduct] = {
            (p.kind, p.category_ext_id, p.offer_ext_id): p
            for p in self.session.execute(
                select(SupplierProduct).where(SupplierProduct.supplier_id == supplier.id)
            ).scalars()
        }
        seen: set[tuple[str, str, str]] = set()

        with SyncRunRecorder(self.session, f"catalog:{self.adapter.code}") as run:
            source = items if items is not None else self.adapter.iter_products()
            for item in source:
                key = (
                    item.kind.value if isinstance(item.kind, ProductKind) else str(item.kind),
                    item.category_ext_id,
                    item.offer_ext_id,
                )
                if key in seen:
                    # The supplier listed the same SKU twice in one pull.  Take
                    # the first and count it -- silently overwriting would hide a
                    # real data problem on their side.
                    run.bump("duplicate_in_feed")
                    continue
                seen.add(key)

                row = existing.get(key)
                if row is None:
                    row = SupplierProduct(
                        supplier_id=supplier.id,
                        kind=key[0],
                        category_ext_id=item.category_ext_id,
                        offer_ext_id=item.offer_ext_id,
                    )
                    self.session.add(row)
                    run.bump("created")
                    changed = True
                else:
                    changed = row.content_hash != item.content_hash()

                if changed:
                    self._apply(row, item)
                    run.bump("updated" if row.id else "created_fields")
                else:
                    run.bump("unchanged")

                row.last_seen_at = now
                row.missing_runs = 0
                if not row.is_active:
                    row.is_active = True
                    run.bump("reactivated")

            # Anything not in this pull.
            for key, row in existing.items():
                if key in seen or not row.is_active:
                    continue
                row.missing_runs += 1
                if row.missing_runs >= self.settings.deactivate_missing_after_runs:
                    row.is_active = False
                    run.bump("deactivated")
                else:
                    run.bump("missing_grace")

            self.session.flush()
            run.bump("total_seen", len(seen))
            return dict(run.stats)

    @staticmethod
    def _apply(row: SupplierProduct, item: SupplierItem) -> None:
        parts = parse_title(item.name)
        row.category_name = item.category_name
        row.name = item.name
        row.name_normalized = parts.core
        row.region = item.region or parts.region
        row.platform = item.platform or parts.platform
        row.region_restricted = item.region_restricted
        row.price_supplier = item.price
        row.supplier_currency = item.currency
        row.stock = max(0, int(item.stock))
        row.min_order_quantity = max(1, int(item.min_order_quantity))
        row.max_order_quantity = max(
            row.min_order_quantity, int(item.max_order_quantity or 1)
        )
        row.content_hash = item.content_hash()
        row.raw = item.raw


class StoreCatalogSync:
    """G2A (or any store) product catalogue -> store_products.

    We mirror the store catalogue because the store's API cannot be searched by
    name.  Matching against a local table turns an impossible query into an
    indexed lookup.
    """

    def __init__(self, session: Session, adapter, *, settings: Settings | None = None):
        self.session = session
        self.adapter = adapter
        self.settings = settings or get_settings()

    def run(
        self,
        *,
        updated_since: dt.datetime | None = None,
        items=None,
        max_pages: int | None = None,
    ) -> dict[str, int]:
        now = dt.datetime.now(dt.timezone.utc)
        store_code = self.adapter.code

        with SyncRunRecorder(self.session, f"store-catalog:{store_code}") as run:
            source = (
                items
                if items is not None
                else self.adapter.iter_catalog(
                    updated_since=updated_since, max_pages=max_pages
                )
            )
            for item in source:
                row = self.session.execute(
                    select(StoreProduct).where(
                        StoreProduct.store_code == store_code,
                        StoreProduct.product_ext_id == item.product_ext_id,
                    )
                ).scalar_one_or_none()
                if row is None:
                    row = StoreProduct(
                        store_code=store_code, product_ext_id=item.product_ext_id
                    )
                    self.session.add(row)
                    run.bump("created")
                else:
                    run.bump("updated")
                self._apply(row, item, now)
            self.session.flush()
            return dict(run.stats)

    @staticmethod
    def _apply(row: StoreProduct, item: StoreCatalogItem, now: dt.datetime) -> None:
        parts = parse_title(item.name)
        row.name = item.name
        row.name_normalized = parts.core
        row.match_key = parts.match_key
        row.slug = item.slug
        row.product_type = item.product_type
        row.region = item.region or parts.region
        row.platform = item.platform or parts.platform
        row.qty = int(item.qty or 0)
        row.min_price = item.min_price
        row.retail_min_price = item.retail_min_price
        row.available_to_buy = bool(item.available_to_buy)
        row.price_limit_min = item.price_limit_min
        row.price_limit_max = item.price_limit_max
        row.remote_updated_at = item.remote_updated_at
        row.raw = item.raw
        row.synced_at = now
