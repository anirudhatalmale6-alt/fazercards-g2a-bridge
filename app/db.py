"""Engine and session plumbing.

The code is deliberately driver-agnostic: PostgreSQL in Docker, SQLite for the
test suite.  Nothing below assumes a Postgres-only feature.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import Base

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def _create_engine(url: str) -> Engine:
    settings = get_settings()
    if url.startswith("sqlite"):
        engine = create_engine(url, future=True, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            # Without this SQLite ignores our ON DELETE CASCADE clauses.
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

        return engine

    return create_engine(
        url,
        future=True,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )


def get_engine() -> Engine:
    global _engine, _SessionFactory
    if _engine is None:
        _engine = _create_engine(get_settings().database_url)
        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _SessionFactory is not None
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transaction boundary: commit on success, roll back on any exception."""
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


def create_all() -> None:
    """Create the schema from the models.

    Used by the test suite and by ``bridge db init``.  Production deployments
    run the SQL in migrations/ instead, which is generated from these models.
    """
    Base.metadata.create_all(get_engine())


def reset_engine(url: str | None = None) -> None:
    """Test hook -- point the process at a different database."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None
    if url is not None:
        import os

        os.environ["DATABASE_URL"] = url
        from app.config import reload_settings

        reload_settings()
