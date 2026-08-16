from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Set before importing app.config so Settings picks these up.
os.environ.setdefault("FAZERCARDS_API_KEY", "fc_test_key")
os.environ.setdefault("G2A_CLIENT_ID", "cid")
os.environ.setdefault("G2A_CLIENT_SECRET", "csecret")
os.environ.setdefault("ENCRYPTION_KEY", "Fh6vQ0Jt9m1kY2pR5sW8xZ3bN7cD4eG6hJ0lK2nM4oQ=")
os.environ.setdefault("FX_RATE_USD_EUR", "0.90")
os.environ.setdefault("LOG_LEVEL", "WARNING")

from app import db as db_module  # noqa: E402
from app.config import reload_settings  # noqa: E402
from app.stores.g2a import G2AAdapter, G2AClient  # noqa: E402
from app.suppliers.fazercards import FazerCardsAdapter, FazerCardsClient  # noqa: E402
from tests.fakes import FakeFazerCards, FakeG2A  # noqa: E402


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path}/bridge.db")
    return reload_settings()


@pytest.fixture
def session(settings):
    db_module.reset_engine()
    db_module.create_all()
    with db_module.session_scope() as s:
        yield s
    db_module.reset_engine()


@pytest.fixture
def no_sleep():
    """Collapse back-off delays so retry tests run instantly.

    The delays themselves are asserted on separately -- see test_http_client.
    """
    recorded: list[float] = []
    return recorded.append, recorded


@pytest.fixture
def fake_supplier():
    return FakeFazerCards()


@pytest.fixture
def fake_store():
    return FakeG2A()


@pytest.fixture
def supplier_adapter(settings, fake_supplier):
    client = FazerCardsClient(
        settings,
        client=httpx.Client(
            transport=fake_supplier.transport(), base_url=settings.fazercards_base_url
        ),
        sleep=lambda _s: None,
    )
    return FazerCardsAdapter(client=client, settings=settings)


@pytest.fixture
def store_adapter(settings, fake_store):
    client = G2AClient(
        settings,
        client=httpx.Client(
            transport=fake_store.transport(), base_url=settings.g2a_base_url
        ),
        sleep=lambda _s: None,
    )
    return G2AAdapter(client=client, settings=settings)
