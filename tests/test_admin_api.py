"""The admin API: auth, the review queue, and the duplicate guard."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.models import MappingStatus, ProductKind, StoreProduct, SupplierProduct
from app.services.catalog_sync import get_or_create_supplier
from app.services.matching import MatchingEngine
from app.services.text import parse_title

TOKEN = "test-admin-token"


@pytest.fixture
def client(session, monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", TOKEN)
    from app.api.main import api

    return TestClient(api)


def _seed(session, supplier_name: str, store_name: str):
    supplier = get_or_create_supplier(session, "fazercards")
    parts = parse_title(supplier_name)
    sp = SupplierProduct(
        supplier_id=supplier.id,
        kind=ProductKind.GAME_KEY.value,
        category_ext_id="c1",
        offer_ext_id="o1",
        name=supplier_name,
        name_normalized=parts.core,
        region=parts.region,
        platform=parts.platform,
        price_supplier=10,
        stock=3,
        content_hash="h1",
    )
    store_parts = parse_title(store_name)
    gp = StoreProduct(
        store_code="g2a",
        product_ext_id="10000000000001",
        name=store_name,
        name_normalized=store_parts.core,
        match_key=store_parts.match_key,
        region=store_parts.region,
        platform=store_parts.platform,
    )
    session.add_all([sp, gp])
    session.flush()
    return sp, gp


def test_health_needs_no_token(client):
    assert client.get("/health").json()["status"] == "ok"


def test_endpoints_reject_a_missing_token(client):
    assert client.get("/status").status_code == 401
    assert client.get("/mappings").status_code == 401


def test_an_unset_admin_token_disables_the_api_rather_than_opening_it(client, monkeypatch):
    """An unset secret must never be read as "no auth required"."""
    monkeypatch.delenv("ADMIN_API_TOKEN", raising=False)
    response = client.get("/status", headers={"X-Admin-Token": "anything"})
    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


def test_review_queue_lists_pending_mappings_with_alternatives(client, session):
    _seed(session, "Cyberpunk 2077", "Cyberpunk 2077 - Steam Key - GLOBAL")
    # A second, near-identical store product forces a review rather than an auto-accept.
    parts = parse_title("Cyberpunk 2077 - Steam Key GLOBAL")
    session.add(
        StoreProduct(
            store_code="g2a",
            product_ext_id="10000000000002",
            name="Cyberpunk 2077 - Steam Key GLOBAL",
            name_normalized=parts.core,
            match_key=parts.match_key,
            region=parts.region,
            platform=parts.platform,
        )
    )
    session.flush()
    MatchingEngine(session).run()
    session.commit()

    rows = client.get("/mappings", headers={"X-Admin-Token": TOKEN}).json()
    assert len(rows) == 1
    assert rows[0]["status"] == MappingStatus.PENDING.value
    assert rows[0]["supplier_name"] == "Cyberpunk 2077"
    assert len(rows[0]["candidates"]) == 2


def test_approving_a_mapping_marks_it_approved(client, session):
    sp, gp = _seed(session, "Cyberpunk 2077", "Cyberpunk 2077 - Steam Key - GLOBAL")
    engine = MatchingEngine(session)
    mapping = engine.apply(sp, engine.evaluate(sp))
    session.commit()

    response = client.post(
        f"/mappings/{mapping.id}/approve",
        json={"reviewed_by": "tester"},
        headers={"X-Admin-Token": TOKEN},
    )
    assert response.status_code == 200
    assert response.json()["status"] == MappingStatus.APPROVED.value


def test_approving_onto_a_claimed_product_returns_409(client, session):
    sp_a, gp = _seed(session, "Cyberpunk 2077", "Cyberpunk 2077 - Steam Key - GLOBAL")
    supplier = get_or_create_supplier(session, "fazercards")
    sp_b = SupplierProduct(
        supplier_id=supplier.id,
        kind=ProductKind.GAME_KEY.value,
        category_ext_id="c2",
        offer_ext_id="o2",
        name="Something Else Entirely",
        name_normalized="something else entirely",
        price_supplier=5,
        stock=1,
        content_hash="h2",
    )
    session.add(sp_b)
    session.flush()

    engine = MatchingEngine(session)
    mapping_a = engine.apply(sp_a, engine.evaluate(sp_a))
    mapping_b = engine.apply(sp_b, engine.evaluate(sp_b))
    session.commit()

    client.post(
        f"/mappings/{mapping_a.id}/approve",
        json={"store_product_id": gp.id},
        headers={"X-Admin-Token": TOKEN},
    )
    response = client.post(
        f"/mappings/{mapping_b.id}/approve",
        json={"store_product_id": gp.id},
        headers={"X-Admin-Token": TOKEN},
    )
    assert response.status_code == 409
    assert "already mapped" in response.json()["detail"]


def test_status_reports_the_shape_of_the_catalogue(client, session):
    _seed(session, "Cyberpunk 2077", "Cyberpunk 2077 - Steam Key - GLOBAL")
    MatchingEngine(session).run()
    session.commit()

    body = client.get("/status", headers={"X-Admin-Token": TOKEN}).json()
    assert body["supplier_products"]["total"] == 1
    assert body["store_products_mirrored"] == 1
    assert body["mappings"]["auto"] == 1
