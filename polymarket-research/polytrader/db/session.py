"""Database engine & session management (SQLite default, PostgreSQL-ready)."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import get_config
from .models import Base

_engine: Optional[Engine] = None
_SessionFactory: Optional[sessionmaker] = None


def _make_engine(url: str) -> Engine:
    is_sqlite = url.startswith("sqlite")
    if is_sqlite:
        # ensure the parent directory for the .db file exists
        path = url.replace("sqlite:///", "")
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, future=True, connect_args={"check_same_thread": False})

        # SQLite needs PRAGMAs for FK enforcement and concurrent reads.
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

        return engine
    # PostgreSQL / other
    return create_engine(url, future=True, pool_pre_ping=True)


def get_engine() -> Engine:
    global _engine, _SessionFactory
    if _engine is None:
        url = get_config().database_url
        _engine = _make_engine(url)
        _SessionFactory = sessionmaker(bind=_engine, future=True, expire_on_commit=False)
    return _engine


def init_db(drop: bool = False) -> None:
    """Create all tables (optionally dropping first for a clean rebuild)."""
    engine = get_engine()
    if drop:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session context manager."""
    if _SessionFactory is None:
        get_engine()
    assert _SessionFactory is not None
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """Drop cached engine (used by tests switching DATABASE_URL)."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None
