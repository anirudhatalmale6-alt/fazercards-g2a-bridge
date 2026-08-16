"""Catalogue pull, offer push, and the rules that keep them cheap and correct.

These run the real adapters against the fakes, so a wrong request payload fails
here rather than on the live account.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import (
    MappingStatus,
    OfferState,
    PriceChangeLog,
    StoreOffer,
    SupplierProduct,
)
from app.services.catalog_sync import StoreCatalogSync, SupplierCatalogSync
from app.services.matching import MatchingEngine
from app.services.offer_sync import OfferSync
from app.services.pricing import Pricer


def seed_supplier(fake):
    fake.add_game(
        "g-1",
        "Xbox Game Pass Essential 6 Months",
        [{"key_id": "k-1", "name": "6 Months", "price_usd": "20.00", "stock": 7,
          "min_order_quantity": 1, "max_order_quantity": 10}],
        region="INDIA",
        platform="xbox",
    )
    fake.add_game(
        "g-2",
        "Warhammer 40,000: Rogue Trader - Season Pass 2",
        [{"key_id": "k-1", "name": "Season Pass 2", "price_usd": "31.50", "stock": 3,
          "min_order_quantity": 1, "max_order_quantity": 5}],
        region="GLOBAL",
        platform="steam",
    )
    fake.add_gift_category(
        "c-1",
        "Roblox Card",
        [
            {"card_id": "d-350", "name": "350 HKD", "price_usd": "45.00", "stock": 12,
             "min_order_quantity": 1, "max_order_quantity": 10},
            {"card_id": "d-100", "name": "100 HKD", "price_usd": "13.00", "stock": 4,
             "min_order_quantity": 1, "max_order_quantity": 10},
        ],
    )


def seed_store(fake):
    fake.add_product(
        "10000000000001",
        "Xbox Game Pass Essential 6 Months - Xbox Live Key - INDIA",
        region="INDIA", platform="Xbox Live",
    )
    fake.add_product(
        "10000000000002",
        "Warhammer 40,000: Rogue Trader - Season Pass 2 (PC) - Steam Key - GLOBAL",
        region="GLOBAL", platform="Steam",
    )
    fake.add_product(
        "10000000000003",
        "Roblox Card 350 HKD - Roblox Key - HONG KONG",
        region="HONG KONG", platform="Roblox",
    )
    fake.add_product(
        "10000000000004",
        "Roblox Card 100 HKD - Roblox Key - HONG KONG",
        region="HONG KONG", platform="Roblox",
    )


@pytest.fixture
def wired(session, fake_supplier, fake_store, supplier_adapter, store_adapter):
    seed_supplier(fake_supplier)
    seed_store(fake_store)
    SupplierCatalogSync(session, supplier_adapter).run()
    StoreCatalogSync(session, store_adapter).run()
    MatchingEngine(session).run()
    return session, fake_supplier, fake_store, supplier_adapter, store_adapter


# ------------------------------------------------------------------ catalogue


def test_supplier_catalogue_lands_in_the_database(session, fake_supplier, supplier_adapter):
    seed_supplier(fake_supplier)
    stats = SupplierCatalogSync(session, supplier_adapter).run()
    assert stats["created"] == 4  # 2 game keys + 2 gift card denominations
    rows = session.execute(select(SupplierProduct)).scalars().all()
    # The offer name is folded into the category name rather than repeated:
    # "Xbox Game Pass Essential 6 Months" + "6 Months" is not "... 6 Months 6 Months".
    assert {r.name for r in rows} == {
        "Xbox Game Pass Essential 6 Months",
        "Warhammer 40,000: Rogue Trader - Season Pass 2",
        "Roblox Card 350 HKD",
        "Roblox Card 100 HKD",
    }


def test_rerunning_the_catalogue_sync_updates_instead_of_duplicating(
    session, fake_supplier, supplier_adapter
):
    seed_supplier(fake_supplier)
    sync = SupplierCatalogSync(session, supplier_adapter)
    sync.run()
    before = session.execute(select(func.count(SupplierProduct.id))).scalar_one()

    fake_supplier.games["g-1"]["keys"][0]["price_usd"] = "22.50"
    stats = sync.run()

    after = session.execute(select(func.count(SupplierProduct.id))).scalar_one()
    assert after == before
    assert stats["updated"] == 1
    assert stats["unchanged"] == 3
    row = session.execute(
        select(SupplierProduct).where(SupplierProduct.category_ext_id == "g-1")
    ).scalar_one()
    assert Decimal(str(row.price_supplier)) == Decimal("22.50")


def test_a_vanished_product_is_not_deactivated_on_the_first_miss(
    session, fake_supplier, supplier_adapter, settings
):
    """One flaky supplier response must not wipe the catalogue."""
    seed_supplier(fake_supplier)
    sync = SupplierCatalogSync(session, supplier_adapter)
    sync.run()

    del fake_supplier.games["g-2"]
    stats = sync.run()
    assert stats.get("missing_grace") == 1
    assert stats.get("deactivated") is None

    stats = sync.run()  # second consecutive miss
    assert stats["deactivated"] == 1
    row = session.execute(
        select(SupplierProduct).where(SupplierProduct.category_ext_id == "g-2")
    ).scalar_one()
    assert row.is_active is False


def test_a_returning_product_is_reactivated(session, fake_supplier, supplier_adapter):
    seed_supplier(fake_supplier)
    sync = SupplierCatalogSync(session, supplier_adapter)
    sync.run()
    saved = fake_supplier.games.pop("g-2")
    sync.run()
    sync.run()
    fake_supplier.games["g-2"] = saved
    stats = sync.run()
    assert stats["reactivated"] == 1


def test_supplier_pagination_is_followed(session, fake_supplier, supplier_adapter):
    for i in range(45):
        fake_supplier.add_game(
            f"bulk-{i}",
            f"Bulk Game {i}",
            [{"key_id": "k", "name": "Standard", "price_usd": "5.00", "stock": 1,
              "min_order_quantity": 1, "max_order_quantity": 1}],
        )
    fake_supplier.page_size = 10
    stats = SupplierCatalogSync(session, supplier_adapter).run()
    assert stats["created"] == 45


# -------------------------------------------------------------------- mapping


def test_matching_pairs_the_right_products(wired):
    session = wired[0]
    from app.models import ProductMapping, StoreProduct

    rows = session.execute(
        select(SupplierProduct.name, StoreProduct.name)
        .join(ProductMapping, ProductMapping.supplier_product_id == SupplierProduct.id)
        .join(StoreProduct, ProductMapping.store_product_id == StoreProduct.id)
        .where(ProductMapping.status.in_([MappingStatus.AUTO.value, MappingStatus.APPROVED.value]))
    ).all()
    pairs = {s: g for s, g in rows}
    assert pairs["Roblox Card 350 HKD"] == "Roblox Card 350 HKD - Roblox Key - HONG KONG"
    assert pairs["Roblox Card 100 HKD"] == "Roblox Card 100 HKD - Roblox Key - HONG KONG"
    assert (
        pairs["Xbox Game Pass Essential 6 Months"]
        == "Xbox Game Pass Essential 6 Months - Xbox Live Key - INDIA"
    )


# --------------------------------------------------------------------- offers


def test_offers_are_created_with_price_and_stock(wired):
    session, _fake_supplier, fake_store, _sa, store_adapter = wired
    stats = OfferSync(session, store_adapter).run()
    assert stats["created"] >= 3

    # 20.00 USD x 0.90 FX x 1.15 markup = 20.70
    xbox = next(
        o for o in fake_store.offers.values() if o["product"]["id"] == "10000000000001"
    )
    assert xbox["price"] == "20.70"
    assert xbox["inventory"]["size"] == 7
    assert xbox["status"] == "active"


def test_a_second_run_with_no_changes_sends_no_requests(wired):
    session, _fs, fake_store, _sa, store_adapter = wired
    sync = OfferSync(session, store_adapter)
    sync.run()
    before = len(fake_store.requests)

    stats = sync.run()

    assert stats["skipped_unchanged"] == stats["examined"]
    assert len(fake_store.requests) == before, "an unchanged sync must not call the API"


def test_stock_change_is_pushed(wired):
    session, fake_supplier, fake_store, supplier_adapter, store_adapter = wired
    OfferSync(session, store_adapter).run()

    fake_supplier.games["g-1"]["keys"][0]["stock"] = 2
    SupplierCatalogSync(session, supplier_adapter).run()
    stats = OfferSync(session, store_adapter).run()

    assert stats["updated"] == 1
    xbox = next(o for o in fake_store.offers.values() if o["product"]["id"] == "10000000000001")
    assert xbox["inventory"]["size"] == 2


def test_out_of_stock_deactivates_rather_than_deleting(wired):
    session, fake_supplier, fake_store, supplier_adapter, store_adapter = wired
    OfferSync(session, store_adapter).run()

    fake_supplier.games["g-1"]["keys"][0]["stock"] = 0
    SupplierCatalogSync(session, supplier_adapter).run()
    OfferSync(session, store_adapter).run()

    xbox = next(o for o in fake_store.offers.values() if o["product"]["id"] == "10000000000001")
    assert xbox["status"] == "inactive"
    assert xbox["inventory"]["size"] == 0
    # The offer row survives, so the offer comes straight back when stock does.
    offer = session.execute(
        select(StoreOffer).where(StoreOffer.product_ext_id == "10000000000001")
    ).scalar_one()
    assert offer.state == OfferState.OUT_OF_STOCK.value
    assert offer.offer_ext_id is not None


def test_stock_returning_reactivates_the_same_offer(wired):
    session, fake_supplier, fake_store, supplier_adapter, store_adapter = wired
    OfferSync(session, store_adapter).run()
    offer_id_before = session.execute(
        select(StoreOffer.offer_ext_id).where(StoreOffer.product_ext_id == "10000000000001")
    ).scalar_one()

    fake_supplier.games["g-1"]["keys"][0]["stock"] = 0
    SupplierCatalogSync(session, supplier_adapter).run()
    OfferSync(session, store_adapter).run()

    fake_supplier.games["g-1"]["keys"][0]["stock"] = 9
    SupplierCatalogSync(session, supplier_adapter).run()
    OfferSync(session, store_adapter).run()

    xbox = next(o for o in fake_store.offers.values() if o["product"]["id"] == "10000000000001")
    assert xbox["status"] == "active"
    assert xbox["inventory"]["size"] == 9
    offer_id_after = session.execute(
        select(StoreOffer.offer_ext_id).where(StoreOffer.product_ext_id == "10000000000001")
    ).scalar_one()
    assert offer_id_after == offer_id_before, "must reuse the offer, not create a new one"


def test_price_change_budget_defers_instead_of_collecting_429s(wired, settings, monkeypatch):
    """The store caps price changes per product per hour.

    Once our budget is spent we must stop sending price changes -- and crucially
    must not record the unsent price as pushed, or it would never be sent.
    """
    session, fake_supplier, fake_store, supplier_adapter, store_adapter = wired
    fake_store.price_change_limit = 4  # the real cap
    sync = OfferSync(session, store_adapter)
    sync.run()

    budget = settings.effective_price_change_budget  # 5 - 1 safety margin = 4
    prices = ["21.00", "22.00", "23.00", "24.00", "25.00", "26.00"]
    for price in prices:
        fake_supplier.games["g-1"]["keys"][0]["price_usd"] = price
        SupplierCatalogSync(session, supplier_adapter).run()
        sync.run()

    changes = session.execute(
        select(func.count(PriceChangeLog.id)).where(
            PriceChangeLog.product_ext_id == "10000000000001"
        )
    ).scalar_one()
    # One create + (budget - 1) updates before the budget is exhausted.
    assert changes == budget
    # And we never provoked a 429 from the store.
    assert fake_store.price_changes["10000000000001"] <= 4

    offer = session.execute(
        select(StoreOffer).where(StoreOffer.product_ext_id == "10000000000001")
    ).scalar_one()
    latest = Pricer(session).compute(
        session.execute(
            select(SupplierProduct).where(SupplierProduct.category_ext_id == "g-1")
        ).scalar_one()
    )
    assert Decimal(str(offer.pushed_price)) != latest.final_price, (
        "a deferred price must not be recorded as pushed"
    )


def test_a_deferred_price_is_sent_once_the_hour_rolls_over(wired, settings):
    session, fake_supplier, fake_store, supplier_adapter, store_adapter = wired
    sync = OfferSync(session, store_adapter)
    sync.run()

    for price in ["21.00", "22.00", "23.00", "24.00", "25.00"]:
        fake_supplier.games["g-1"]["keys"][0]["price_usd"] = price
        SupplierCatalogSync(session, supplier_adapter).run()
        sync.run()

    # Age the log out of the rolling window, as an hour passing would.
    for row in session.execute(select(PriceChangeLog)).scalars():
        row.changed_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
    session.flush()
    fake_store.price_changes.clear()

    stats = sync.run()
    assert stats["updated"] == 1
    xbox = next(o for o in fake_store.offers.values() if o["product"]["id"] == "10000000000001")
    assert xbox["price"] == "25.88"  # 25.00 x 0.90 x 1.15


def test_a_rejected_offer_is_not_treated_as_success(wired):
    """202 means "queued", not "done" -- a job that fails must clear our state."""
    session, _fs, fake_store, _sa, store_adapter = wired
    fake_store.fail_job_for_product.add("10000000000002")

    stats = OfferSync(session, store_adapter).run()

    assert stats["errors"] == 1
    offer = session.execute(
        select(StoreOffer).where(StoreOffer.product_ext_id == "10000000000002")
    ).scalar_one()
    assert offer.state == OfferState.FAILED.value
    assert "OFFER_REJECTED" in offer.last_error
    assert offer.pushed_price is None, "a failed push must not be remembered as pushed"


def test_a_job_still_running_is_resolved_on_the_next_pass(wired):
    """When the store has not finished the job, we wait rather than re-create."""
    session, fake_supplier, fake_store, supplier_adapter, store_adapter = wired
    fake_store.fail_job_for_product.add("10000000000002")
    fake_store.pending_job_lookups = 99  # the job never reports back
    fake_store.hide_product_offers = True  # and the offer is not queryable yet

    sync = OfferSync(session, store_adapter)
    sync.run()
    offer = session.execute(
        select(StoreOffer).where(StoreOffer.product_ext_id == "10000000000002")
    ).scalar_one()
    assert offer.pending_job_id is not None
    assert offer.offer_ext_id is None

    # Something changes while the job is still running.  We cannot update an
    # offer whose id we do not know -- and must not create a second one.
    fake_supplier.games["g-2"]["keys"][0]["stock"] = 1
    SupplierCatalogSync(session, supplier_adapter).run()
    created_before = len(fake_store.offers)
    stats = sync.run()
    assert stats.get("awaiting_job", 0) == 1
    assert len(fake_store.offers) == created_before

    fake_store.pending_job_lookups = 0
    fake_store.hide_product_offers = False
    stats = sync.reconcile_jobs()
    assert stats["failed"] == 1
    session.refresh(offer)
    assert offer.state == OfferState.FAILED.value
    assert offer.pushed_price is None


def test_offer_ids_come_from_the_job_result(wired):
    session, _fs, _fstore, _sa, store_adapter = wired
    OfferSync(session, store_adapter).run()
    offers = session.execute(select(StoreOffer)).scalars().all()
    assert all(o.offer_ext_id for o in offers)


def test_adopting_offers_that_already_exist_avoids_duplicate_creates(wired):
    """Taking over an account that something else already populated."""
    session, _fs, fake_store, _sa, store_adapter = wired
    OfferSync(session, store_adapter).run()

    # Forget everything we know about the offers, as a fresh deployment would.
    for offer in session.execute(select(StoreOffer)).scalars():
        session.delete(offer)
    session.flush()

    sync = OfferSync(session, store_adapter)
    stats = sync.adopt_existing_offers()
    assert stats["adopted"] >= 3

    created_before = len(fake_store.offers)
    run_stats = sync.run()
    assert len(fake_store.offers) == created_before, "must not re-create existing offers"
    assert run_stats.get("created") is None


def test_dry_run_touches_nothing(wired):
    session, _fs, fake_store, _sa, store_adapter = wired
    before = len(fake_store.requests)
    stats = OfferSync(session, store_adapter).run(dry_run=True)
    assert stats["would_create"] >= 3
    assert len(fake_store.requests) == before
