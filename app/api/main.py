"""Admin HTTP API.

Small on purpose.  It exists so the mapping review queue can be worked through
without touching the database by hand, and so the health of the bridge can be
checked from outside.

It is not a public API: bind it to localhost or put it behind your reverse proxy
and the ``ADMIN_API_TOKEN`` header check below.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_session_factory
from app.logging_conf import configure_logging
from app.models import (
    ApiCall,
    MappingStatus,
    OfferState,
    ProductMapping,
    StoreOffer,
    StoreProduct,
    SupplierProduct,
    SyncRun,
)
from app.services.matching import MatchingEngine, approve, reject

configure_logging()

api = FastAPI(
    title="Supplier-Store Bridge admin API",
    version="1.0.0",
    description="Mapping review and operational status for the FazerCards <-> G2A bridge.",
)


def get_db() -> Session:
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def require_token(x_admin_token: Annotated[str | None, Header()] = None) -> None:
    """Reject anything without the shared admin token.

    If ADMIN_API_TOKEN is unset the API refuses every request rather than
    running wide open -- an unset secret must never mean "no auth needed".
    """
    expected = os.environ.get("ADMIN_API_TOKEN")
    if not expected:
        raise HTTPException(
            503, "ADMIN_API_TOKEN is not configured; the admin API is disabled"
        )
    if x_admin_token != expected:
        raise HTTPException(401, "bad or missing X-Admin-Token")


# ------------------------------------------------------------------ schemas


class MappingOut(BaseModel):
    mapping_id: int
    status: str
    score: float | None = None
    supplier_product_id: int
    supplier_name: str
    supplier_price: float
    supplier_stock: int
    store_product_id: int | None = None
    store_product_ext_id: str | None = None
    store_name: str | None = None
    candidates: list[dict] = Field(default_factory=list)


class ApproveIn(BaseModel):
    store_product_id: int | None = None
    reviewed_by: str = "admin-api"
    note: str | None = None


class RejectIn(BaseModel):
    reviewed_by: str = "admin-api"
    note: str | None = None


class OfferOut(BaseModel):
    """One product as it stands: what it costs us, what it sells for, and why."""

    mapping_id: int
    offer_ext_id: str | None = None
    store_product_ext_id: str
    supplier_name: str
    store_name: str
    state: str
    sync_enabled: bool
    stock: int
    supplier_price: float
    supplier_currency: str
    markup_percent: float
    markup_is_override: bool
    fixed_price_override: float | None = None
    computed_price: float | None = None
    pushed_price: float | None = None
    pushed_inventory: int | None = None
    price_pending: bool = False
    price_changes_this_hour: int = 0
    price_budget: int = 0
    last_pushed_at: dt.datetime | None = None
    last_error: str | None = None


class PricingIn(BaseModel):
    """Per-product pricing.

    All three fields are optional and independent; send only what you want to
    change.  Send markup_percent_override = null to fall back to the global
    default, and fixed_price_override = null to go back to markup pricing.
    """

    markup_percent_override: float | None = Field(default=None, ge=-90, le=1000)
    fixed_price_override: float | None = Field(default=None, ge=0)
    sync_enabled: bool | None = None
    clear_markup_override: bool = False
    clear_fixed_price: bool = False


# ----------------------------------------------------------------- endpoints


_UI_FILE = Path(__file__).with_name("ui.html")


@api.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/ui")


@api.get("/ui", response_class=HTMLResponse, include_in_schema=False)
def control_panel() -> HTMLResponse:
    """The control panel.

    Served without the token check: the page itself contains no data, it asks
    for the token and then calls the same protected endpoints as everything
    else.  Gating the HTML too would only mean nowhere to type the token in.
    """
    return HTMLResponse(_UI_FILE.read_text(encoding="utf-8"))


@api.get("/health", tags=["ops"])
def health(session: Session = Depends(get_db)) -> dict[str, Any]:
    """Liveness plus a real database round-trip.

    Deliberately reports the database as the only hard dependency.  A health
    check that also calls the upstream APIs would flap every time a supplier has
    a bad minute and would make deploys roll back for no reason.
    """
    session.execute(select(func.count(SupplierProduct.id))).scalar_one()
    return {"status": "ok", "time": dt.datetime.now(dt.timezone.utc).isoformat()}


@api.get("/status", tags=["ops"], dependencies=[Depends(require_token)])
def status(session: Session = Depends(get_db)) -> dict[str, Any]:
    """One call that answers "is the bridge actually working"."""
    def count(model, *where):
        stmt = select(func.count(model.id))
        for clause in where:
            stmt = stmt.where(clause)
        return int(session.execute(stmt).scalar_one())

    last_runs = session.execute(
        select(SyncRun).order_by(SyncRun.started_at.desc()).limit(8)
    ).scalars().all()

    return {
        "supplier_products": {
            "total": count(SupplierProduct),
            "active": count(SupplierProduct, SupplierProduct.is_active.is_(True)),
            "in_stock": count(SupplierProduct, SupplierProduct.stock > 0),
        },
        "store_products_mirrored": count(StoreProduct),
        "mappings": {
            status_value.value: count(
                ProductMapping, ProductMapping.status == status_value.value
            )
            for status_value in MappingStatus
        },
        "offers": {
            state.value: count(StoreOffer, StoreOffer.state == state.value)
            for state in OfferState
        },
        "recent_runs": [
            {
                "kind": r.kind,
                "state": r.state,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                "stats": r.stats,
                "error": r.error,
            }
            for r in last_runs
        ],
    }


@api.get(
    "/mappings",
    response_model=list[MappingOut],
    tags=["mapping"],
    dependencies=[Depends(require_token)],
)
def list_mappings(
    session: Session = Depends(get_db),
    status_filter: str = Query(MappingStatus.PENDING.value, alias="status"),
    limit: int = Query(50, le=500),
    offset: int = Query(0, ge=0),
) -> list[MappingOut]:
    rows = session.execute(
        select(ProductMapping, SupplierProduct, StoreProduct)
        .join(SupplierProduct, ProductMapping.supplier_product_id == SupplierProduct.id)
        .outerjoin(StoreProduct, ProductMapping.store_product_id == StoreProduct.id)
        .where(ProductMapping.status == status_filter)
        .order_by(ProductMapping.score.desc().nullslast())
        .limit(limit)
        .offset(offset)
    ).all()
    return [
        MappingOut(
            mapping_id=m.id,
            status=m.status,
            score=m.score,
            supplier_product_id=sp.id,
            supplier_name=sp.name,
            supplier_price=float(sp.price_supplier),
            supplier_stock=sp.stock,
            store_product_id=m.store_product_id,
            store_product_ext_id=g.product_ext_id if g else None,
            store_name=g.name if g else None,
            candidates=m.candidates or [],
        )
        for m, sp, g in rows
    ]


@api.post("/mappings/{mapping_id}/approve", tags=["mapping"], dependencies=[Depends(require_token)])
def approve_mapping(
    mapping_id: int, body: ApproveIn, session: Session = Depends(get_db)
) -> dict[str, Any]:
    try:
        mapping = approve(
            session,
            mapping_id,
            store_product_id=body.store_product_id,
            reviewed_by=body.reviewed_by,
            note=body.note,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        # e.g. the store product is already claimed by another mapping
        raise HTTPException(409, str(exc)) from exc
    return {"mapping_id": mapping.id, "status": mapping.status}


@api.post("/mappings/{mapping_id}/reject", tags=["mapping"], dependencies=[Depends(require_token)])
def reject_mapping(
    mapping_id: int, body: RejectIn, session: Session = Depends(get_db)
) -> dict[str, Any]:
    try:
        mapping = reject(session, mapping_id, reviewed_by=body.reviewed_by, note=body.note)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"mapping_id": mapping.id, "status": mapping.status}


@api.get(
    "/mappings/{mapping_id}/candidates", tags=["mapping"], dependencies=[Depends(require_token)]
)
def mapping_candidates(mapping_id: int, session: Session = Depends(get_db)) -> dict[str, Any]:
    """Re-score this supplier product against the catalogue, right now."""
    mapping = session.get(ProductMapping, mapping_id)
    if mapping is None:
        raise HTTPException(404, "mapping not found")
    supplier_product = session.get(SupplierProduct, mapping.supplier_product_id)
    candidates = MatchingEngine(session).find_candidates(supplier_product)
    return {
        "mapping_id": mapping_id,
        "supplier_name": supplier_product.name,
        "candidates": [c.as_json() for c in candidates],
    }


@api.get(
    "/offers",
    response_model=list[OfferOut],
    tags=["offers"],
    dependencies=[Depends(require_token)],
)
def list_offers(
    session: Session = Depends(get_db),
    search: str | None = Query(None, description="Substring of the supplier or store title"),
    state: str | None = Query(None, description="Filter by offer state"),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
) -> list[OfferOut]:
    """Every mapped product with its full price breakdown.

    This is the "what am I selling and for how much" screen.  It shows the
    supplier cost, the markup actually in force, the price the bridge computes
    from them, and the price the store is currently holding -- so a difference
    between the last two is visible rather than something you have to infer.
    """
    from app.config import get_settings
    from app.http.ratelimit import PriceChangeBudget
    from app.services.pricing import Pricer, PricingError

    settings = get_settings()
    stmt = (
        select(ProductMapping, SupplierProduct, StoreProduct, StoreOffer)
        .join(SupplierProduct, ProductMapping.supplier_product_id == SupplierProduct.id)
        .join(StoreProduct, ProductMapping.store_product_id == StoreProduct.id)
        .outerjoin(StoreOffer, StoreOffer.mapping_id == ProductMapping.id)
        .where(
            ProductMapping.status.in_(
                [MappingStatus.AUTO.value, MappingStatus.APPROVED.value]
            )
        )
    )
    if search:
        pattern = f"%{search.lower()}%"
        stmt = stmt.where(
            func.lower(SupplierProduct.name).like(pattern)
            | func.lower(StoreProduct.name).like(pattern)
        )
    if state:
        stmt = stmt.where(StoreOffer.state == state)

    rows = session.execute(
        stmt.order_by(SupplierProduct.name).limit(limit).offset(offset)
    ).all()

    pricer = Pricer(session, settings=settings)
    budget = PriceChangeBudget(
        session, store_code="g2a", limit=settings.effective_price_change_budget
    )

    out: list[OfferOut] = []
    for mapping, supplier_product, store_product, offer in rows:
        try:
            breakdown = pricer.compute(supplier_product, mapping, store_product)
            computed = float(breakdown.final_price)
        except PricingError:
            # A missing FX rate must not blank the whole screen -- show the rest.
            computed = None
        pushed = float(offer.pushed_price) if offer and offer.pushed_price else None
        out.append(
            OfferOut(
                mapping_id=mapping.id,
                offer_ext_id=offer.offer_ext_id if offer else None,
                store_product_ext_id=store_product.product_ext_id,
                supplier_name=supplier_product.name,
                store_name=store_product.name,
                state=offer.state if offer else "pending",
                sync_enabled=mapping.sync_enabled,
                stock=supplier_product.stock,
                supplier_price=float(supplier_product.price_supplier),
                supplier_currency=supplier_product.supplier_currency,
                markup_percent=(
                    mapping.markup_percent_override
                    if mapping.markup_percent_override is not None
                    else settings.default_markup_percent
                ),
                markup_is_override=mapping.markup_percent_override is not None,
                fixed_price_override=(
                    float(mapping.fixed_price_override)
                    if mapping.fixed_price_override is not None
                    else None
                ),
                computed_price=computed,
                pushed_price=pushed,
                pushed_inventory=offer.pushed_inventory if offer else None,
                price_pending=(
                    computed is not None and pushed is not None and abs(computed - pushed) > 0.001
                ),
                price_changes_this_hour=budget.used(store_product.product_ext_id),
                price_budget=settings.effective_price_change_budget,
                last_pushed_at=offer.last_pushed_at if offer else None,
                last_error=offer.last_error if offer else None,
            )
        )
    return out


@api.post("/mappings/{mapping_id}/pricing", tags=["offers"], dependencies=[Depends(require_token)])
def set_pricing(
    mapping_id: int, body: PricingIn, session: Session = Depends(get_db)
) -> dict[str, Any]:
    """Change what one product sells for.

    The new price is not pushed here -- it takes effect on the next offer sync,
    which is also what keeps us inside the store's price-change budget.  The
    response returns the price that will be sent so the change can be checked
    before it goes anywhere.
    """
    from app.services.pricing import Pricer, PricingError

    mapping = session.get(ProductMapping, mapping_id)
    if mapping is None:
        raise HTTPException(404, "mapping not found")

    if body.clear_markup_override:
        mapping.markup_percent_override = None
    elif body.markup_percent_override is not None:
        mapping.markup_percent_override = body.markup_percent_override

    if body.clear_fixed_price:
        mapping.fixed_price_override = None
    elif body.fixed_price_override is not None:
        mapping.fixed_price_override = body.fixed_price_override

    if body.sync_enabled is not None:
        mapping.sync_enabled = body.sync_enabled
    session.flush()

    supplier_product = session.get(SupplierProduct, mapping.supplier_product_id)
    store_product = (
        session.get(StoreProduct, mapping.store_product_id)
        if mapping.store_product_id
        else None
    )
    try:
        breakdown = Pricer(session).compute(supplier_product, mapping, store_product)
        preview: dict[str, Any] = {
            "supplier_price": float(breakdown.supplier_price),
            "fx_rate": float(breakdown.fx_rate),
            "markup_percent": float(breakdown.markup_percent),
            "new_price": float(breakdown.final_price),
            "clamped": breakdown.clamped,
            "floored": breakdown.floored,
            "reason": breakdown.reason,
        }
    except PricingError as exc:
        preview = {"error": str(exc)}

    return {
        "mapping_id": mapping.id,
        "sync_enabled": mapping.sync_enabled,
        "markup_percent_override": mapping.markup_percent_override,
        "fixed_price_override": (
            float(mapping.fixed_price_override)
            if mapping.fixed_price_override is not None
            else None
        ),
        "preview": preview,
        "note": "Applied on the next offer sync.",
    }


@api.get("/settings", tags=["ops"], dependencies=[Depends(require_token)])
def current_settings() -> dict[str, Any]:
    """The pricing and scheduling knobs currently in force.

    Values only -- no secrets.  Useful for confirming that a .env change was
    actually picked up by the running container.
    """
    from app.config import get_settings

    settings = get_settings()
    return {
        "default_markup_percent": settings.default_markup_percent,
        "supplier_currency": settings.supplier_currency,
        "store_currency": settings.store_currency,
        "fx_rate_pinned": settings.fx_rate_usd_eur,
        "price_rounding": settings.price_rounding,
        "min_offer_price": settings.min_offer_price,
        "price_stock_sync_minutes": settings.price_stock_sync_minutes,
        "catalog_sync_minutes": settings.catalog_sync_minutes,
        "price_change_budget_per_product_per_hour": settings.effective_price_change_budget,
        "match_auto_accept_score": settings.match_auto_accept_score,
        "match_review_score": settings.match_review_score,
    }


@api.get("/logs/api-calls", tags=["ops"], dependencies=[Depends(require_token)])
def api_calls(
    session: Session = Depends(get_db),
    service: str | None = None,
    failed_only: bool = False,
    limit: int = Query(50, le=500),
) -> list[dict[str, Any]]:
    stmt = select(ApiCall).order_by(ApiCall.created_at.desc()).limit(limit)
    if service:
        stmt = stmt.where(ApiCall.service == service)
    if failed_only:
        stmt = stmt.where(
            (ApiCall.error.is_not(None)) | (ApiCall.status_code >= 400)
        )
    return [
        {
            "id": row.id,
            "service": row.service,
            "method": row.method,
            "url": row.url,
            "attempt": row.attempt,
            "status_code": row.status_code,
            "duration_ms": row.duration_ms,
            "error": row.error,
            "correlation_id": row.correlation_id,
            "created_at": row.created_at,
        }
        for row in session.execute(stmt).scalars()
    ]
