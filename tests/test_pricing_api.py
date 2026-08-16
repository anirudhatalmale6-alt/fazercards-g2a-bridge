"""The offers screen and per-product pricing controls."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.models import ProductKind, StoreProduct, SupplierProduct
from app.services.catalog_sync import get_or_create_supplier
from app.services.matching import MatchingEngine
from app.services.offer_sync import OfferSync
from app.services.text import parse_title
from tests.test_sync_flow import seed_store, seed_supplier

TOKEN = "test-admin-token"
HEAD = {"X-Admin-Token": TOKEN}


@pytest.fixture
def client(session, monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", TOKEN)
    from app.api.main import api

    return TestClient(api)


@pytest.fixture
def mapped(session, fake_supplier, fake_store, supplier_adapter, store_adapter):
    from app.services.catalog_sync import StoreCatalogSync, SupplierCatalogSync

    seed_supplier(fake_supplier)
    seed_store(fake_store)
    SupplierCatalogSync(session, supplier_adapter).run()
    StoreCatalogSync(session, store_adapter).run()
    MatchingEngine(session).run()
    session.commit()
    return session, store_adapter, fake_store


def _find(rows, needle):
    return next(r for r in rows if needle in r["supplier_name"])


def test_offers_screen_shows_the_full_price_breakdown(client, mapped):
    rows = client.get("/offers", headers=HEAD).json()
    xbox = _find(rows, "Xbox Game Pass")

    # 20.00 USD cost, 0.90 FX, 15% markup -> 20.70 EUR
    assert xbox["supplier_price"] == 20.0
    assert xbox["supplier_currency"] == "USD"
    assert xbox["markup_percent"] == 15.0
    assert xbox["markup_is_override"] is False
    assert xbox["computed_price"] == 20.70
    assert xbox["stock"] == 7
    assert xbox["state"] == "pending"  # nothing pushed yet
    assert xbox["pushed_price"] is None


def test_setting_a_markup_changes_the_computed_price(client, mapped):
    rows = client.get("/offers", headers=HEAD).json()
    mapping_id = _find(rows, "Xbox Game Pass")["mapping_id"]

    response = client.post(
        f"/mappings/{mapping_id}/pricing",
        json={"markup_percent_override": 40},
        headers=HEAD,
    )
    assert response.status_code == 200
    # 20.00 x 0.90 x 1.40 = 25.20
    assert response.json()["preview"]["new_price"] == 25.20

    rows = client.get("/offers", headers=HEAD).json()
    xbox = _find(rows, "Xbox Game Pass")
    assert xbox["markup_percent"] == 40.0
    assert xbox["markup_is_override"] is True
    assert xbox["computed_price"] == 25.20


def test_a_markup_override_only_affects_its_own_product(client, mapped):
    rows = client.get("/offers", headers=HEAD).json()
    mapping_id = _find(rows, "Xbox Game Pass")["mapping_id"]
    client.post(
        f"/mappings/{mapping_id}/pricing", json={"markup_percent_override": 40}, headers=HEAD
    )

    rows = client.get("/offers", headers=HEAD).json()
    others = [r for r in rows if "Xbox Game Pass" not in r["supplier_name"]]
    assert others, "expected other mapped products"
    assert all(r["markup_percent"] == 15.0 for r in others)


def test_a_fixed_price_overrides_the_markup_entirely(client, mapped):
    rows = client.get("/offers", headers=HEAD).json()
    mapping_id = _find(rows, "Roblox Card 350")["mapping_id"]

    response = client.post(
        f"/mappings/{mapping_id}/pricing",
        json={"fixed_price_override": 59.99},
        headers=HEAD,
    )
    assert response.json()["preview"]["new_price"] == 59.99

    rows = client.get("/offers", headers=HEAD).json()
    roblox = _find(rows, "Roblox Card 350")
    assert roblox["fixed_price_override"] == 59.99
    assert roblox["computed_price"] == 59.99


def test_clearing_a_fixed_price_returns_to_markup_pricing(client, mapped):
    rows = client.get("/offers", headers=HEAD).json()
    mapping_id = _find(rows, "Roblox Card 350")["mapping_id"]
    client.post(
        f"/mappings/{mapping_id}/pricing", json={"fixed_price_override": 59.99}, headers=HEAD
    )

    response = client.post(
        f"/mappings/{mapping_id}/pricing", json={"clear_fixed_price": True}, headers=HEAD
    )
    # 45.00 x 0.90 x 1.15 = 46.5750 -> 46.58
    assert response.json()["preview"]["new_price"] == 46.58

    rows = client.get("/offers", headers=HEAD).json()
    assert _find(rows, "Roblox Card 350")["fixed_price_override"] is None


def test_a_price_change_is_not_pushed_until_the_next_sync(client, mapped):
    """Saving a price must not fire an API call -- that is what respects the cap."""
    session, store_adapter, fake_store = mapped
    OfferSync(session, store_adapter).run()
    session.commit()

    rows = client.get("/offers", headers=HEAD).json()
    xbox = _find(rows, "Xbox Game Pass")
    assert xbox["pushed_price"] == 20.70
    assert xbox["price_pending"] is False

    requests_before = len(fake_store.requests)
    client.post(
        f"/mappings/{xbox['mapping_id']}/pricing",
        json={"markup_percent_override": 40},
        headers=HEAD,
    )
    assert len(fake_store.requests) == requests_before, "saving must not call the store"

    rows = client.get("/offers", headers=HEAD).json()
    xbox = _find(rows, "Xbox Game Pass")
    assert xbox["computed_price"] == 25.20
    assert xbox["pushed_price"] == 20.70
    assert xbox["price_pending"] is True, "the screen must show the change is not live yet"

    # The next sync is what actually sends it.
    OfferSync(session, store_adapter).run()
    session.commit()
    rows = client.get("/offers", headers=HEAD).json()
    xbox = _find(rows, "Xbox Game Pass")
    assert xbox["pushed_price"] == 25.20
    assert xbox["price_pending"] is False


def test_pausing_a_product_stops_it_being_synced(client, mapped):
    session, store_adapter, _fake_store = mapped
    rows = client.get("/offers", headers=HEAD).json()
    mapping_id = _find(rows, "Xbox Game Pass")["mapping_id"]

    client.post(f"/mappings/{mapping_id}/pricing", json={"sync_enabled": False}, headers=HEAD)

    stats = OfferSync(session, store_adapter).run()
    session.commit()
    assert stats["examined"] == 3, "the paused product must be left out entirely"

    rows = client.get("/offers", headers=HEAD).json()
    assert _find(rows, "Xbox Game Pass")["sync_enabled"] is False


def test_search_filters_the_offers_list(client, mapped):
    rows = client.get("/offers?search=roblox", headers=HEAD).json()
    assert rows
    assert all("Roblox" in r["supplier_name"] for r in rows)


def test_pricing_endpoint_rejects_an_absurd_markup(client, mapped):
    rows = client.get("/offers", headers=HEAD).json()
    mapping_id = rows[0]["mapping_id"]
    response = client.post(
        f"/mappings/{mapping_id}/pricing",
        json={"markup_percent_override": 100000},
        headers=HEAD,
    )
    assert response.status_code == 422


def test_pricing_endpoint_404s_on_an_unknown_mapping(client, mapped):
    response = client.post("/mappings/999999/pricing", json={"sync_enabled": True}, headers=HEAD)
    assert response.status_code == 404


def test_settings_endpoint_exposes_values_but_no_secrets(client, mapped):
    body = client.get("/settings", headers=HEAD).json()
    assert body["default_markup_percent"] == 15.0
    assert body["price_change_budget_per_product_per_hour"] == 4
    serialised = str(body).lower()
    for secret in ("fc_test_key", "csecret", "client_secret", "api_key"):
        assert secret not in serialised


def test_offers_and_pricing_require_the_token(client, mapped):
    assert client.get("/offers").status_code == 401
    assert client.post("/mappings/1/pricing", json={}).status_code == 401


def test_the_control_panel_page_is_served(client):
    response = client.get("/ui")
    assert response.status_code == 200
    assert "Bridge control panel" in response.text
    # Self-contained: no external stylesheet, script or image to fetch, so the
    # panel renders on a server with no outbound internet access.
    import re

    body = response.text
    assert not re.search(r"<script[^>]+\bsrc\s*=", body)
    assert not re.search(r"<link[^>]+\bhref\s*=", body)
    assert not re.search(r"<img[^>]+\bsrc\s*=\s*[\"']https?:", body)
    assert "@import" not in body
