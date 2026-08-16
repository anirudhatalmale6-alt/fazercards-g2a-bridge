"""The contract every supplier adapter implements.

The rest of the bridge only ever sees ``SupplierItem`` and ``PurchaseResult``.
Adding a second supplier therefore means writing one class here and inserting a
row in ``suppliers`` -- no changes to mapping, pricing, offers or fulfilment.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Protocol, runtime_checkable

from app.models import ProductKind


@dataclass(frozen=True)
class SupplierItem:
    """One buyable SKU, normalised across suppliers."""

    kind: ProductKind
    category_ext_id: str
    offer_ext_id: str
    name: str
    price: Decimal
    stock: int
    currency: str = "USD"
    category_name: str | None = None
    region: str | None = None
    platform: str | None = None
    region_restricted: bool = False
    min_order_quantity: int = 1
    max_order_quantity: int = 1
    raw: dict = field(default_factory=dict)

    def content_hash(self) -> str:
        """Digest of the fields that, when changed, require a push to the store.

        ``raw`` is deliberately excluded: suppliers like to add cosmetic fields,
        and we do not want an image URL change to burn a price-change slot.
        """
        payload = json.dumps(
            {
                "name": self.name,
                "price": str(self.price),
                "currency": self.currency,
                "stock": self.stock,
                "region": self.region,
                "platform": self.platform,
                "min": self.min_order_quantity,
                "max": self.max_order_quantity,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class PurchasedCode:
    value: str
    kind: str = "text"


@dataclass(frozen=True)
class PurchaseResult:
    supplier_order_ext_id: str
    codes: tuple[PurchasedCode, ...]
    cost: Decimal | None = None
    currency: str = "USD"
    status: str = "completed"
    raw: dict = field(default_factory=dict)

    @property
    def is_complete(self) -> bool:
        return bool(self.codes)


class SupplierUnavailable(RuntimeError):
    """Supplier is up but cannot fulfil right now (out of stock, low balance)."""


class OutOfStock(SupplierUnavailable):
    pass


class InsufficientBalance(SupplierUnavailable):
    pass


@runtime_checkable
class SupplierAdapter(Protocol):
    code: str

    def iter_products(self) -> Iterable[SupplierItem]:
        """Yield the full catalogue, paging internally."""

    def purchase(
        self,
        item: SupplierItem,
        quantity: int,
        *,
        idempotency_key: str,
    ) -> PurchaseResult:
        """Buy `quantity` of `item`.

        Implementations MUST pass ``idempotency_key`` upstream so that a retry
        of this exact call returns the original purchase rather than making a
        second one.
        """

    def get_order(self, order_ext_id: str) -> PurchaseResult | None:
        """Re-read a past order -- used to recover after a timeout mid-purchase."""

    def balance(self) -> Decimal | None:
        """Account balance, or None if the supplier does not expose one."""
