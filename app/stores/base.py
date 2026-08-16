"""The contract every store adapter implements."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Protocol, runtime_checkable


@dataclass(frozen=True)
class StoreCatalogItem:
    """A product in the store's catalogue that we could attach an offer to."""

    product_ext_id: str
    name: str
    product_type: str | None = None
    slug: str | None = None
    region: str | None = None
    platform: str | None = None
    qty: int = 0
    min_price: Decimal | None = None
    retail_min_price: Decimal | None = None
    available_to_buy: bool = True
    price_limit_min: Decimal | None = None
    price_limit_max: Decimal | None = None
    remote_updated_at: dt.datetime | None = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class StoreOfferView:
    """An offer as the store currently sees it."""

    offer_ext_id: str
    product_ext_id: str
    price: Decimal | None
    inventory_size: int
    status: str
    offer_type: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True)
class StoreOrderView:
    order_ext_id: str
    status: str
    items: tuple[dict, ...] = ()
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class JobResult:
    """Stores accept writes asynchronously and hand back a job id."""

    job_id: str
    status: str = "pending"
    resource_id: str | None = None
    errors: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict)

    @property
    def is_finished(self) -> bool:
        return self.status in {"complete", "failed", "error"}

    @property
    def succeeded(self) -> bool:
        return self.status == "complete" and not self.errors


@runtime_checkable
class StoreAdapter(Protocol):
    code: str

    def iter_catalog(
        self, *, updated_since: dt.datetime | None = None, include_out_of_stock: bool = True
    ) -> Iterable[StoreCatalogItem]: ...

    def list_offers(self, *, offer_type: str = "dropshipping") -> Iterable[StoreOfferView]: ...

    def create_offer(
        self, *, product_ext_id: str, price: Decimal, inventory_size: int, active: bool = True
    ) -> JobResult: ...

    def update_offer(
        self,
        offer_ext_id: str,
        *,
        price: Decimal | None = None,
        inventory_size: int | None = None,
        active: bool | None = None,
        archive: bool | None = None,
    ) -> JobResult: ...

    def get_job(self, job_id: str) -> JobResult: ...
