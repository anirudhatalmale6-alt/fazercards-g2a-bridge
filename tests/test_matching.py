"""Title parsing, scoring, and the rules that stop duplicates."""

from __future__ import annotations

import pytest

from app.models import MappingStatus, ProductKind, StoreProduct, SupplierProduct
from app.services import matching as matching_mod
from app.services.matching import MatchingEngine, approve, reject, score_pair
from app.services.text import extract_denomination, extract_region, parse_title


# --------------------------------------------------------------------- text


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Roblox Card 350 HKD - Roblox Key - HONG KONG", (350.0, "HKD")),
        ("Steam Gift Card 50 USD", (50.0, "USD")),
        ("$25 PlayStation Network Card", (25.0, "USD")),
        ("Amazon Gift Card 20€", (20.0, "EUR")),
        ("Xbox Game Pass Essential 6 Months - Xbox Live Key - INDIA", None),
        ("Warhammer 40,000: Rogue Trader - Season Pass 2 (PC) - Steam Key - GLOBAL", None),
    ],
)
def test_denomination_extraction(title, expected):
    assert extract_denomination(title) == expected


@pytest.mark.parametrize(
    "title,expected",
    [
        ("... - Steam Key - GLOBAL", "GLOBAL"),
        ("Xbox Game Pass - Xbox Live Key - INDIA", "INDIA"),
        ("Roblox Card 350 HKD - HONG KONG", "HONG KONG"),
        ("Some Game - Steam Key", None),
    ],
)
def test_region_extraction(title, expected):
    assert extract_region(title) == expected


def test_supplier_and_store_titles_for_the_same_product_score_high():
    supplier = parse_title("Xbox Game Pass Essential 6 Months INDIA")
    store = parse_title("Xbox Game Pass Essential 6 Months - Xbox Live Key - INDIA")
    score, _reasons = score_pair(supplier, store)
    assert score >= 0.92


def test_different_denominations_are_rejected_outright():
    """The expensive mistake: selling a 100 HKD card as a 350 HKD one."""
    supplier = parse_title("Roblox Card 350 HKD")
    store = parse_title("Roblox Card 100 HKD - Roblox Key - HONG KONG")
    score, reasons = score_pair(supplier, store)
    assert score == 0.0
    assert "denomination mismatch" in reasons


def test_different_regions_are_rejected_outright():
    supplier = parse_title("Xbox Game Pass Essential 6 Months INDIA")
    store = parse_title("Xbox Game Pass Essential 6 Months - Xbox Live Key - TURKEY")
    score, reasons = score_pair(supplier, store)
    assert score == 0.0
    assert any("region mismatch" in r for r in reasons)


def test_different_platforms_are_rejected_outright():
    supplier = parse_title("Grand Theft Auto V - Steam - GLOBAL")
    store = parse_title("Grand Theft Auto V - Rockstar Key - GLOBAL")
    score, reasons = score_pair(supplier, store)
    assert score == 0.0
    assert any("platform mismatch" in r for r in reasons)


def test_unrelated_products_score_low():
    supplier = parse_title("Warhammer 40,000: Rogue Trader Season Pass 2")
    store = parse_title("Cyberpunk 2077 - Steam Key - GLOBAL")
    score, _ = score_pair(supplier, store)
    assert score < 0.70


# ------------------------------------------------------------------ engine


def _supplier_product(session, name, **kw):
    from app.services.catalog_sync import get_or_create_supplier

    supplier = get_or_create_supplier(session, "fazercards")
    parts = parse_title(name)
    row = SupplierProduct(
        supplier_id=supplier.id,
        kind=kw.pop("kind", ProductKind.GAME_KEY.value),
        category_ext_id=kw.pop("category_ext_id", f"cat-{name[:8]}"),
        offer_ext_id=kw.pop("offer_ext_id", "off-1"),
        name=name,
        name_normalized=parts.core,
        region=parts.region,
        platform=parts.platform,
        price_supplier=kw.pop("price", 10),
        stock=kw.pop("stock", 5),
        content_hash=kw.pop("content_hash", name),
        **kw,
    )
    session.add(row)
    session.flush()
    return row


def _store_product(session, product_ext_id, name):
    parts = parse_title(name)
    row = StoreProduct(
        store_code="g2a",
        product_ext_id=product_ext_id,
        name=name,
        name_normalized=parts.core,
        match_key=parts.match_key,
        region=parts.region,
        platform=parts.platform,
    )
    session.add(row)
    session.flush()
    return row


def test_engine_auto_accepts_a_confident_match(session):
    sp = _supplier_product(session, "Xbox Game Pass Essential 6 Months INDIA")
    store = _store_product(
        session, "10000004143007", "Xbox Game Pass Essential 6 Months - Xbox Live Key - INDIA"
    )

    engine = MatchingEngine(session)
    outcome = engine.evaluate(sp)
    assert outcome.status is MappingStatus.AUTO
    assert outcome.best.store_product_id == store.id


def test_engine_queues_a_near_tie_for_review(session):
    """Two candidates within a hair of each other is not a certainty."""
    sp = _supplier_product(session, "Cyberpunk 2077")
    _store_product(session, "10000000000001", "Cyberpunk 2077 - Steam Key - GLOBAL")
    _store_product(session, "10000000000002", "Cyberpunk 2077 - Steam Key GLOBAL")

    engine = MatchingEngine(session)
    outcome = engine.evaluate(sp)
    assert outcome.status is MappingStatus.PENDING
    assert len(outcome.candidates) == 2


def test_engine_reports_unmatched_when_nothing_is_close(session):
    sp = _supplier_product(session, "Some Obscure Indie Game Deluxe")
    _store_product(session, "10000000000003", "FIFA 26 - Steam Key - GLOBAL")

    outcome = MatchingEngine(session).evaluate(sp)
    assert outcome.status is MappingStatus.UNMATCHED
    assert outcome.best is None


def test_a_store_product_can_only_be_claimed_once(session):
    """The dedup guarantee: two supplier SKUs cannot both map to one product."""
    first = _supplier_product(
        session, "Xbox Game Pass Essential 6 Months INDIA", offer_ext_id="off-1"
    )
    second = _supplier_product(
        session, "Xbox Game Pass Essential 6 Months INDIA", offer_ext_id="off-2"
    )
    _store_product(
        session, "10000004143007", "Xbox Game Pass Essential 6 Months - Xbox Live Key - INDIA"
    )

    engine = MatchingEngine(session)
    engine.apply(first, engine.evaluate(first))
    outcome = engine.evaluate(second)

    assert outcome.best is None
    assert outcome.status is MappingStatus.UNMATCHED


def test_rejected_pairs_are_never_suggested_again(session):
    sp = _supplier_product(session, "Xbox Game Pass Essential 6 Months INDIA")
    _store_product(
        session, "10000004143007", "Xbox Game Pass Essential 6 Months - Xbox Live Key - INDIA"
    )

    engine = MatchingEngine(session)
    mapping = engine.apply(sp, engine.evaluate(sp))
    reject(session, mapping.id, note="wrong edition")

    fresh = MatchingEngine(session)
    outcome = fresh.evaluate(sp)
    assert outcome.best is None


def test_approval_refuses_to_steal_another_mappings_product(session):
    a = _supplier_product(session, "Cyberpunk 2077", offer_ext_id="a")
    b = _supplier_product(session, "Totally Different Title", offer_ext_id="b")
    store = _store_product(session, "10000000000009", "Cyberpunk 2077 - Steam Key - GLOBAL")

    engine = MatchingEngine(session)
    mapping_a = engine.apply(a, engine.evaluate(a))
    approve(session, mapping_a.id, store_product_id=store.id)

    mapping_b = engine.apply(b, engine.evaluate(b))
    with pytest.raises(ValueError, match="already mapped"):
        approve(session, mapping_b.id, store_product_id=store.id)


def test_human_decisions_survive_a_rematch(session):
    sp = _supplier_product(session, "Cyberpunk 2077")
    store = _store_product(session, "10000000000010", "Cyberpunk 2077 - Steam Key - GLOBAL")

    engine = MatchingEngine(session)
    mapping = engine.apply(sp, engine.evaluate(sp))
    approve(session, mapping.id, store_product_id=store.id, note="checked by hand")

    MatchingEngine(session).run(only_unmapped=False)
    session.refresh(mapping)
    assert mapping.status == MappingStatus.APPROVED.value
    assert mapping.note == "checked by hand"


def test_run_counts_each_outcome(session):
    _supplier_product(session, "Xbox Game Pass Essential 6 Months INDIA", offer_ext_id="o1")
    _supplier_product(session, "Nothing Like Anything", offer_ext_id="o2")
    _store_product(
        session, "10000004143007", "Xbox Game Pass Essential 6 Months - Xbox Live Key - INDIA"
    )

    counters = MatchingEngine(session).run()
    assert counters["examined"] == 2
    assert counters["auto"] == 1
    assert counters["unmatched"] == 1
