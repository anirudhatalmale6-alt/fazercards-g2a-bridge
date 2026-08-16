"""Database schema.

These models are the single source of truth for the schema.  The committed SQL
in migrations/ is generated from them (``python -m app.tools.dump_schema``), so
the two can never drift.

Design notes that matter:

* ``supplier_products`` is keyed on the supplier's own identifiers, so re-running
  a catalogue pull updates rows instead of inserting new ones.
* ``product_mappings`` carries a *unique* constraint on both sides.  One supplier
  SKU can map to at most one store product and vice versa -- that single
  constraint is what makes duplicate offers impossible rather than unlikely.
* ``store_offers`` remembers what we last pushed, so a sync that would change
  nothing sends no request at all.
* ``idempotency_keys`` is what stops a retried or replayed order from buying a
  second key.
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _now_col() -> Mapped[dt.datetime]:
    return mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# --------------------------------------------------------------------- enums


class ProductKind(str, enum.Enum):
    GAME_KEY = "game_key"
    GIFT_CARD = "gift_card"


class MappingStatus(str, enum.Enum):
    PENDING = "pending"  # matched by the engine, waiting for a human
    APPROVED = "approved"  # a human said yes
    AUTO = "auto"  # score was high enough to skip review
    REJECTED = "rejected"  # a human said no -- never re-suggest
    UNMATCHED = "unmatched"  # engine found nothing good enough


class OfferState(str, enum.Enum):
    PENDING = "pending"  # never pushed
    LIVE = "live"
    OUT_OF_STOCK = "out_of_stock"
    ARCHIVED = "archived"
    FAILED = "failed"


class OrderState(str, enum.Enum):
    RECEIVED = "received"
    RESERVED = "reserved"
    PURCHASING = "purchasing"
    PURCHASED = "purchased"
    DELIVERED = "delivered"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class RunState(str, enum.Enum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


# ----------------------------------------------------------------- suppliers


class Supplier(Base):
    """One row per upstream supplier.

    The bridge is built against FazerCards first, but everything downstream
    (mapping, pricing, offers, orders) joins through this table, so adding a
    second supplier is a new adapter plus a row -- not a rewrite.
    """

    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Per-supplier knobs (markup override, excluded categories, ...).
    settings: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[dt.datetime] = _now_col()

    products: Mapped[list["SupplierProduct"]] = relationship(back_populates="supplier")


class SupplierProduct(Base):
    """A buyable SKU on the supplier side.

    ``category_ext_id`` + ``offer_ext_id`` is FazerCards' (game_id, key_id) for
    game keys and (category_id, card_id) for gift cards.  Both are needed to
    order, and ``offer_ext_id`` is only unique inside its category -- which the
    unique constraint below encodes.
    """

    __tablename__ = "supplier_products"
    __table_args__ = (
        UniqueConstraint(
            "supplier_id",
            "kind",
            "category_ext_id",
            "offer_ext_id",
            name="uq_supplier_product_identity",
        ),
        Index("ix_supplier_products_active", "supplier_id", "is_active"),
        Index("ix_supplier_products_name_norm", "name_normalized"),
        CheckConstraint("stock >= 0", name="ck_supplier_product_stock_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    supplier_id: Mapped[int] = mapped_column(
        ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)

    category_ext_id: Mapped[str] = mapped_column(String(128), nullable=False)
    offer_ext_id: Mapped[str] = mapped_column(String(128), nullable=False)

    category_name: Mapped[str | None] = mapped_column(String(512))
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    name_normalized: Mapped[str] = mapped_column(String(512), nullable=False)

    region: Mapped[str | None] = mapped_column(String(64))
    platform: Mapped[str | None] = mapped_column(String(64))
    region_restricted: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    price_supplier: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    supplier_currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    stock: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    min_order_quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    max_order_quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # Hash of the fields we care about.  If it is unchanged since the last pull
    # there is nothing to recompute and nothing to push.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    missing_runs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    first_seen_at: Mapped[dt.datetime] = _now_col()
    last_seen_at: Mapped[dt.datetime] = _now_col()
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    supplier: Mapped[Supplier] = relationship(back_populates="products")
    mapping: Mapped["ProductMapping | None"] = relationship(
        back_populates="supplier_product", uselist=False
    )


# --------------------------------------------------------------- store side


class StoreProduct(Base):
    """A product in the store's own catalogue (G2A's 14-digit productId).

    G2A's product list has no name search -- only paging -- so we mirror it
    locally and match against this table.  That is the whole reason a database
    sits in the middle: without it every match would be a full catalogue crawl.
    """

    __tablename__ = "store_products"
    __table_args__ = (
        UniqueConstraint("store_code", "product_ext_id", name="uq_store_product_identity"),
        Index("ix_store_products_name_norm", "name_normalized"),
        Index("ix_store_products_match_key", "store_code", "match_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_code: Mapped[str] = mapped_column(String(32), default="g2a", nullable=False)
    product_ext_id: Mapped[str] = mapped_column(String(32), nullable=False)

    name: Mapped[str] = mapped_column(String(512), nullable=False)
    name_normalized: Mapped[str] = mapped_column(String(512), nullable=False)
    # Normalized name + platform + region -- the bucket we look candidates up in.
    match_key: Mapped[str] = mapped_column(String(600), nullable=False)

    slug: Mapped[str | None] = mapped_column(String(512))
    product_type: Mapped[str | None] = mapped_column(String(64))
    region: Mapped[str | None] = mapped_column(String(64))
    platform: Mapped[str | None] = mapped_column(String(64))

    qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    min_price: Mapped[float | None] = mapped_column(Numeric(12, 4))
    retail_min_price: Mapped[float | None] = mapped_column(Numeric(12, 4))
    available_to_buy: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    price_limit_min: Mapped[float | None] = mapped_column(Numeric(12, 4))
    price_limit_max: Mapped[float | None] = mapped_column(Numeric(12, 4))

    remote_updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    synced_at: Mapped[dt.datetime] = _now_col()


class ProductMapping(Base):
    """The link that keeps everything aligned across updates.

    Both foreign keys are unique: a supplier SKU maps to one store product, and
    a store product is claimed by one supplier SKU.  Two suppliers offering the
    same game cannot both create an offer for it.
    """

    __tablename__ = "product_mappings"
    __table_args__ = (
        UniqueConstraint("supplier_product_id", name="uq_mapping_supplier_product"),
        UniqueConstraint(
            "store_code", "store_product_id", name="uq_mapping_store_product"
        ),
        Index("ix_mappings_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    supplier_product_id: Mapped[int] = mapped_column(
        ForeignKey("supplier_products.id", ondelete="CASCADE"), nullable=False
    )
    store_code: Mapped[str] = mapped_column(String(32), default="g2a", nullable=False)
    store_product_id: Mapped[int | None] = mapped_column(
        ForeignKey("store_products.id", ondelete="SET NULL")
    )

    status: Mapped[str] = mapped_column(
        String(16), default=MappingStatus.PENDING.value, nullable=False
    )
    score: Mapped[float | None] = mapped_column(Float)
    method: Mapped[str | None] = mapped_column(String(32))
    # Runners-up, kept so the review screen can show alternatives.
    candidates: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    markup_percent_override: Mapped[float | None] = mapped_column(Float)
    fixed_price_override: Mapped[float | None] = mapped_column(Numeric(12, 4))
    sync_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    reviewed_by: Mapped[str | None] = mapped_column(String(128))
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[dt.datetime] = _now_col()
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    supplier_product: Mapped[SupplierProduct] = relationship(back_populates="mapping")
    store_product: Mapped[StoreProduct | None] = relationship()
    offer: Mapped["StoreOffer | None"] = relationship(
        back_populates="mapping", uselist=False
    )


class StoreOffer(Base):
    """Our live offer on the store, plus what we last pushed to it.

    ``pushed_*`` is compared against the freshly computed values before every
    sync; equal means we skip the call entirely, which is what keeps us inside
    the price-change rate limit.
    """

    __tablename__ = "store_offers"
    __table_args__ = (
        UniqueConstraint("mapping_id", name="uq_offer_mapping"),
        UniqueConstraint("store_code", "offer_ext_id", name="uq_offer_ext_id"),
        Index("ix_offers_state", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mapping_id: Mapped[int] = mapped_column(
        ForeignKey("product_mappings.id", ondelete="CASCADE"), nullable=False
    )
    store_code: Mapped[str] = mapped_column(String(32), default="g2a", nullable=False)
    # Null until the store's async job reports the created offer id back.
    offer_ext_id: Mapped[str | None] = mapped_column(String(64))
    product_ext_id: Mapped[str] = mapped_column(String(32), nullable=False)

    state: Mapped[str] = mapped_column(
        String(16), default=OfferState.PENDING.value, nullable=False
    )
    active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    pushed_price: Mapped[float | None] = mapped_column(Numeric(12, 4))
    pushed_inventory: Mapped[int | None] = mapped_column(Integer)
    pushed_active: Mapped[bool | None] = mapped_column(Boolean)
    last_pushed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    pending_job_id: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[dt.datetime] = _now_col()
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    mapping: Mapped[ProductMapping] = relationship(back_populates="offer")


class PriceChangeLog(Base):
    """One row per accepted price change, used to enforce the store's cap.

    G2A allows a small number of price changes per product per hour; we count
    our own changes here and defer the rest instead of collecting 429s.
    """

    __tablename__ = "price_change_log"
    __table_args__ = (
        Index("ix_price_change_product_time", "store_code", "product_ext_id", "changed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_code: Mapped[str] = mapped_column(String(32), default="g2a", nullable=False)
    product_ext_id: Mapped[str] = mapped_column(String(32), nullable=False)
    offer_ext_id: Mapped[str | None] = mapped_column(String(64))
    old_price: Mapped[float | None] = mapped_column(Numeric(12, 4))
    new_price: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    changed_at: Mapped[dt.datetime] = _now_col()


# --------------------------------------------------------------- order flow


class StoreOrder(Base):
    """An order taken on the store side.

    ``order_ext_id`` is unique, so the same order arriving twice (webhook replay
    plus a poll, say) can only ever create one row.
    """

    __tablename__ = "store_orders"
    __table_args__ = (
        UniqueConstraint("store_code", "order_ext_id", name="uq_store_order_identity"),
        Index("ix_store_orders_state", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_code: Mapped[str] = mapped_column(String(32), default="g2a", nullable=False)
    order_ext_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reservation_ext_id: Mapped[str | None] = mapped_column(String(128))

    state: Mapped[str] = mapped_column(
        String(16), default=OrderState.RECEIVED.value, nullable=False
    )
    source: Mapped[str] = mapped_column(String(16), default="poll", nullable=False)
    raw: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    received_at: Mapped[dt.datetime] = _now_col()
    delivered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class OrderItem(Base):
    """One line of an order: what we must buy upstream and hand back."""

    __tablename__ = "order_items"
    __table_args__ = (
        UniqueConstraint("order_id", "line_ref", name="uq_order_item_line"),
        Index("ix_order_items_state", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("store_orders.id", ondelete="CASCADE"), nullable=False
    )
    line_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    mapping_id: Mapped[int | None] = mapped_column(
        ForeignKey("product_mappings.id", ondelete="SET NULL")
    )
    product_ext_id: Mapped[str | None] = mapped_column(String(32))
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    state: Mapped[str] = mapped_column(
        String(16), default=OrderState.RECEIVED.value, nullable=False
    )
    # The key that guarantees "buy once", stored before the purchase is made.
    purchase_idempotency_key: Mapped[str | None] = mapped_column(String(128))
    supplier_order_ext_id: Mapped[str | None] = mapped_column(String(128))
    supplier_cost: Mapped[float | None] = mapped_column(Numeric(12, 4))
    keys_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    delivered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[dt.datetime] = _now_col()

    order: Mapped[StoreOrder] = relationship(back_populates="items")
    keys: Mapped[list["DeliveredKey"]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )


class DeliveredKey(Base):
    """A purchased code.

    The code itself is stored encrypted (see app/crypto.py); ``fingerprint`` is a
    salted hash used for de-duplication so we never hand the same code to two
    customers.
    """

    __tablename__ = "delivered_keys"
    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_delivered_key_fingerprint"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_item_id: Mapped[int] = mapped_column(
        ForeignKey("order_items.id", ondelete="CASCADE"), nullable=False
    )
    code_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="text", nullable=False)
    delivered_to_store: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    created_at: Mapped[dt.datetime] = _now_col()

    item: Mapped[OrderItem] = relationship(back_populates="keys")


class IdempotencyRecord(Base):
    """Request-level replay protection.

    A caller (or a retry of our own) that presents a key we have already
    completed gets the stored response back instead of a second side effect.
    """

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("scope", "key", name="uq_idempotency_scope_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="in_progress", nullable=False)
    response: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[dt.datetime] = _now_col()
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


# ------------------------------------------------------------- observability


class SyncRun(Base):
    """One execution of a sync job, with its counters."""

    __tablename__ = "sync_runs"
    __table_args__ = (Index("ix_sync_runs_kind_started", "kind", "started_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), default=RunState.RUNNING.value, nullable=False
    )
    stats: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[dt.datetime] = _now_col()
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class ApiCall(Base):
    """Every outbound (and inbound) API call, with secrets scrubbed.

    This is the log the client asked for: when something goes wrong upstream we
    can point at the exact request, attempt number and response body.
    """

    __tablename__ = "api_calls"
    __table_args__ = (
        Index("ix_api_calls_service_time", "service", "created_at"),
        Index("ix_api_calls_correlation", "correlation_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), default="out", nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    request_body: Mapped[str | None] = mapped_column(Text)
    response_body: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = _now_col()


class FxRate(Base):
    """Cached FX quote, so a broken FX provider cannot silently reprice everything."""

    __tablename__ = "fx_rates"
    __table_args__ = (Index("ix_fx_pair_time", "base", "quote", "fetched_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    base: Mapped[str] = mapped_column(String(3), nullable=False)
    quote: Mapped[str] = mapped_column(String(3), nullable=False)
    rate: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    fetched_at: Mapped[dt.datetime] = _now_col()
