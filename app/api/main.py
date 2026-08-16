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
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
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


# ----------------------------------------------------------------- endpoints


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
