"""Command line interface.

    python -m app.cli --help

Every long-running job is exposed here as well as in the scheduler, so anything
the worker does automatically can be run by hand when investigating.
"""

from __future__ import annotations

import json
from typing import Optional

import typer

from app.config import get_settings
from app.db import create_all, session_scope
from app.logging_conf import configure_logging, get_logger

app = typer.Typer(add_completion=False, help="FazerCards <-> G2A bridge")
db_app = typer.Typer(help="Database maintenance")
sync_app = typer.Typer(help="Catalogue and offer synchronisation")
map_app = typer.Typer(help="Product mapping and review")
app.add_typer(db_app, name="db")
app.add_typer(sync_app, name="sync")
app.add_typer(map_app, name="map")

logger = get_logger(__name__)


def _supplier_adapter():
    from app.suppliers.fazercards import FazerCardsAdapter

    return FazerCardsAdapter()


def _store_adapter():
    from app.stores.g2a import G2AAdapter

    return G2AAdapter()


def _echo(payload) -> None:
    typer.echo(json.dumps(payload, indent=2, default=str))


# ------------------------------------------------------------------ database


@db_app.command("init")
def db_init() -> None:
    """Create every table (development shortcut for the SQL in migrations/)."""
    configure_logging()
    create_all()
    typer.echo("schema created")


@db_app.command("check")
def db_check() -> None:
    """Verify the database is reachable and report row counts."""
    from sqlalchemy import func, select

    from app.models import ProductMapping, StoreOffer, StoreProduct, SupplierProduct

    with session_scope() as session:
        counts = {
            "supplier_products": session.execute(
                select(func.count(SupplierProduct.id))
            ).scalar_one(),
            "store_products": session.execute(
                select(func.count(StoreProduct.id))
            ).scalar_one(),
            "mappings": session.execute(select(func.count(ProductMapping.id))).scalar_one(),
            "offers": session.execute(select(func.count(StoreOffer.id))).scalar_one(),
        }
    _echo(counts)


@app.command("keygen")
def keygen() -> None:
    """Generate an ENCRYPTION_KEY for the .env file."""
    from app.crypto import generate_key

    typer.echo(generate_key())


@app.command("doctor")
def doctor() -> None:
    """Check configuration and reach both APIs.

    Run this first after deploying -- it fails loudly on a missing credential or
    a wrong base URL instead of leaving a worker to retry quietly forever.
    """
    configure_logging()
    settings = get_settings()
    report: dict[str, object] = {"environment": settings.environment}

    try:
        settings.require_fazercards_key()
        report["fazercards_key"] = "present"
        report["fazercards_balance"] = str(_supplier_adapter().balance())
    except Exception as exc:
        report["fazercards"] = f"FAILED: {exc}"

    try:
        settings.require_g2a_credentials()
        adapter = _store_adapter()
        adapter.client.token()
        report["g2a_token"] = "obtained"
        first = next(iter(adapter.iter_catalog(max_pages=1)), None)
        report["g2a_catalog_sample"] = first.name if first else "(empty page)"
    except Exception as exc:
        report["g2a"] = f"FAILED: {exc}"

    try:
        from app.crypto import encrypt

        encrypt("probe")
        report["encryption"] = "configured"
    except Exception as exc:
        report["encryption"] = f"FAILED: {exc}"

    _echo(report)


# ---------------------------------------------------------------------- sync


@sync_app.command("supplier")
def sync_supplier() -> None:
    """Pull the supplier catalogue into the local database."""
    configure_logging()
    from app.services.catalog_sync import SupplierCatalogSync

    with session_scope() as session:
        _echo(SupplierCatalogSync(session, _supplier_adapter()).run())


@sync_app.command("store-catalog")
def sync_store_catalog(
    max_pages: Optional[int] = typer.Option(
        None, help="Stop after N pages. Omit for a full crawl."
    ),
    since_hours: Optional[int] = typer.Option(
        None, help="Only products updated in the last N hours (much faster)."
    ),
) -> None:
    """Mirror the store's product catalogue so matching has something to match against."""
    configure_logging()
    import datetime as dt

    from app.services.catalog_sync import StoreCatalogSync

    updated_since = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=since_hours)
        if since_hours
        else None
    )
    with session_scope() as session:
        _echo(
            StoreCatalogSync(session, _store_adapter()).run(
                updated_since=updated_since, max_pages=max_pages
            )
        )


@sync_app.command("offers")
def sync_offers(
    limit: Optional[int] = typer.Option(None, help="Only process N mappings."),
    dry_run: bool = typer.Option(False, help="Report what would change, send nothing."),
) -> None:
    """Push prices and stock to the store."""
    configure_logging()
    from app.services.offer_sync import OfferSync

    with session_scope() as session:
        sync = OfferSync(session, _store_adapter())
        result = sync.run(limit=limit, dry_run=dry_run)
        if not dry_run:
            result["jobs"] = sync.reconcile_jobs()
        _echo(result)


@sync_app.command("adopt")
def sync_adopt() -> None:
    """Attach offers that already exist on the store to our mappings."""
    configure_logging()
    from app.services.offer_sync import OfferSync

    with session_scope() as session:
        _echo(OfferSync(session, _store_adapter()).adopt_existing_offers())


# ------------------------------------------------------------------- mapping


@map_app.command("run")
def map_run(
    limit: Optional[int] = typer.Option(None),
    all_products: bool = typer.Option(
        False, "--all", help="Re-examine mapped products too (human decisions are kept)."
    ),
) -> None:
    """Match unmapped supplier products against the store catalogue."""
    configure_logging()
    from app.services.matching import MatchingEngine

    with session_scope() as session:
        _echo(MatchingEngine(session).run(limit=limit, only_unmapped=not all_products))


@map_app.command("pending")
def map_pending(limit: int = typer.Option(20)) -> None:
    """List mappings waiting for a human decision."""
    from sqlalchemy import select

    from app.models import MappingStatus, ProductMapping, StoreProduct, SupplierProduct

    with session_scope() as session:
        rows = session.execute(
            select(ProductMapping, SupplierProduct, StoreProduct)
            .join(SupplierProduct, ProductMapping.supplier_product_id == SupplierProduct.id)
            .outerjoin(StoreProduct, ProductMapping.store_product_id == StoreProduct.id)
            .where(ProductMapping.status == MappingStatus.PENDING.value)
            .limit(limit)
        ).all()
        _echo(
            [
                {
                    "mapping_id": m.id,
                    "score": round(m.score, 3) if m.score else None,
                    "supplier": sp.name,
                    "suggested": g.name if g else None,
                    "alternatives": [c["name"] for c in (m.candidates or [])[1:4]],
                }
                for m, sp, g in rows
            ]
        )


@map_app.command("approve")
def map_approve(
    mapping_id: int,
    store_product_id: Optional[int] = typer.Option(None),
    by: str = typer.Option("cli"),
) -> None:
    from app.services.matching import approve

    with session_scope() as session:
        mapping = approve(
            session, mapping_id, store_product_id=store_product_id, reviewed_by=by
        )
        _echo({"mapping_id": mapping.id, "status": mapping.status})


@map_app.command("reject")
def map_reject(mapping_id: int, note: Optional[str] = typer.Option(None), by: str = "cli") -> None:
    from app.services.matching import reject

    with session_scope() as session:
        mapping = reject(session, mapping_id, reviewed_by=by, note=note)
        _echo({"mapping_id": mapping.id, "status": mapping.status})


@map_app.command("price")
def map_price(
    mapping_id: int,
    markup: Optional[float] = typer.Option(None, help="Markup %% for this product only."),
    fixed: Optional[float] = typer.Option(None, help="Sell at exactly this price."),
    clear: bool = typer.Option(False, help="Drop the overrides, use the global markup."),
    pause: bool = typer.Option(False, help="Stop syncing this product."),
    resume: bool = typer.Option(False, help="Resume syncing this product."),
) -> None:
    """Set the price of one product.

    Takes effect on the next offer sync, not immediately -- which is what keeps
    the bridge inside the store's price-change budget.
    """
    from app.models import ProductMapping, StoreProduct, SupplierProduct
    from app.services.pricing import Pricer

    with session_scope() as session:
        mapping = session.get(ProductMapping, mapping_id)
        if mapping is None:
            typer.echo(f"mapping {mapping_id} not found", err=True)
            raise typer.Exit(1)
        if clear:
            mapping.markup_percent_override = None
            mapping.fixed_price_override = None
        if markup is not None:
            mapping.markup_percent_override = markup
        if fixed is not None:
            mapping.fixed_price_override = fixed
        if pause:
            mapping.sync_enabled = False
        if resume:
            mapping.sync_enabled = True
        session.flush()

        supplier_product = session.get(SupplierProduct, mapping.supplier_product_id)
        store_product = (
            session.get(StoreProduct, mapping.store_product_id)
            if mapping.store_product_id
            else None
        )
        breakdown = Pricer(session).compute(supplier_product, mapping, store_product)
        _echo(
            {
                "mapping_id": mapping.id,
                "product": supplier_product.name,
                "sync_enabled": mapping.sync_enabled,
                "cost": float(breakdown.supplier_price),
                "fx_rate": float(breakdown.fx_rate),
                "markup_percent": float(breakdown.markup_percent),
                "new_price": float(breakdown.final_price),
                "applied": "on the next offer sync",
            }
        )


@map_app.command("list")
def map_list(
    search: Optional[str] = typer.Option(None, help="Substring of the product name."),
    limit: int = typer.Option(30),
) -> None:
    """Show mapped products with their cost, markup and sell price."""
    from sqlalchemy import func, select

    from app.config import get_settings
    from app.models import MappingStatus, ProductMapping, StoreProduct, SupplierProduct
    from app.services.pricing import Pricer

    settings = get_settings()
    with session_scope() as session:
        stmt = (
            select(ProductMapping, SupplierProduct, StoreProduct)
            .join(SupplierProduct, ProductMapping.supplier_product_id == SupplierProduct.id)
            .join(StoreProduct, ProductMapping.store_product_id == StoreProduct.id)
            .where(
                ProductMapping.status.in_(
                    [MappingStatus.AUTO.value, MappingStatus.APPROVED.value]
                )
            )
        )
        if search:
            stmt = stmt.where(func.lower(SupplierProduct.name).like(f"%{search.lower()}%"))
        pricer = Pricer(session, settings=settings)
        rows = session.execute(stmt.limit(limit)).all()
        _echo(
            [
                {
                    "mapping_id": m.id,
                    "product": sp.name,
                    "stock": sp.stock,
                    "cost_usd": float(sp.price_supplier),
                    "markup": (
                        m.markup_percent_override
                        if m.markup_percent_override is not None
                        else settings.default_markup_percent
                    ),
                    "sell_price": float(pricer.compute(sp, m, g).final_price),
                    "sync_enabled": m.sync_enabled,
                }
                for m, sp, g in rows
            ]
        )


@app.command("worker")
def worker() -> None:
    """Run the scheduled jobs in the foreground (what the container starts)."""
    from app.worker import run_forever

    run_forever()


if __name__ == "__main__":
    app()
