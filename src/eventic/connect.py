"""``connect(url)`` — the one-engine process registry.

Replaces ``Eventic.init``/``init_eventic`` for the DBOS-free core (I6). The
registry is a single module-level engine; the default persistence plugin and
every read/write go through ``engine()``.

DBOS is never imported here — when the optional ``eventic.dbos`` adapter is
active it reuses this engine rather than creating a second one.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url

from .errors import NotConnected
from .models import Base

_ENGINE: Engine | None = None


def _normalize_pg(url: str) -> str:
    """Postgres URLs ride psycopg3 (the same driver DBOS uses)."""
    u = make_url(url)
    if u.drivername.startswith("postgresql") and u.drivername != "postgresql+psycopg":
        u = u.set(drivername="postgresql+psycopg")
    return str(u)


def connect(url: str, *, create_tables: bool = True) -> None:
    """Wire the process engine. Re-connecting swaps the engine (idempotent-ish).

    ``create_tables`` is a dev convenience — Alembic is the source of truth in
    production (mirrors the 0.1 ``EVENTIC_AUTO_CREATE_TABLES`` escape hatch).
    """
    global _ENGINE
    if _ENGINE is not None:
        _ENGINE.dispose()  # release pooled connections before swapping
    _ENGINE = create_engine(_normalize_pg(url), future=True, pool_pre_ping=True)
    if create_tables:
        Base.metadata.create_all(_ENGINE)


def engine() -> Engine:
    """The process engine — raises :class:`NotConnected` before ``connect()``."""
    if _ENGINE is None:
        raise NotConnected("call eventic.connect(url) first")
    return _ENGINE


def _reset() -> None:
    """Test hook: tear the registry down so the next test starts unconnected."""
    global _ENGINE
    if _ENGINE is not None:
        _ENGINE.dispose()
    _ENGINE = None
