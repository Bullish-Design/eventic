"""``Store`` — the explicit, context-bound object (Step 3).

Replaces v2's ``connect()`` engine registry. All operational state lives on a
``Store`` (I8); the active store is bound per-context with a ``ContextVar`` —
async-safe, thread-safe, and scope exit unbinds, so **no ``_reset`` hook is
needed** (that is how I8 gets satisfied rather than asserted).

``_begin()`` is the integration point that replaces the global ambient-session
hook: an integration (``DbosStore``) subclasses ``Store`` and returns a foreign
session; it does not mutate process state.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator, Self

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from ..errors import NotConnected
from .schema import Base
from .sql import SqlStore
from .unit_of_work import UnitOfWork, _Nested

_ACTIVE: contextvars.ContextVar["Store | None"] = contextvars.ContextVar(
    "eventic_store", default=None
)


def _normalize_pg(url: str) -> str:
    """Postgres URLs ride psycopg3 (the same driver DBOS uses)."""
    u = make_url(url)
    if u.drivername.startswith("postgresql") and u.drivername != "postgresql+psycopg":
        u = u.set(drivername="postgresql+psycopg")
    return str(u)


class Store:
    """One database: the log, the head, and the outbox."""

    def __init__(self, url: str, *, create_tables: bool = False):
        self.url = url
        self.engine = create_engine(_normalize_pg(url), future=True, pool_pre_ping=True)
        if create_tables:
            Base.metadata.create_all(self.engine)

    # -- transaction boundary ------------------------------------------ #
    def _begin(self) -> tuple[Session, bool]:
        """(session, do_we_own_the_commit). Overridden by integrations."""
        return Session(self.engine, future=True), True

    def unit_of_work(self) -> UnitOfWork | _Nested:
        """The transaction boundary. Nested calls stage into the parent."""
        if (cur := UnitOfWork.current()) is not None:
            return _Nested(cur)
        session, owns = self._begin()
        return UnitOfWork(session, owns_commit=owns)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """A session for reads (and writes that commit themselves)."""
        with Session(self.engine, future=True) as s:
            yield s

    # -- scoping -------------------------------------------------------- #
    def activate(self) -> Self:
        """Bind as the active store; returns self for chaining."""
        self._token = _ACTIVE.set(self)
        return self

    def deactivate(self) -> None:
        _ACTIVE.reset(self._token)

    def __enter__(self) -> Self:
        return self.activate()

    def __exit__(self, *exc) -> bool:
        self.deactivate()
        return False


def active_store() -> Store:
    """The active store, or a loud error (F8's no-op era is over)."""
    if (s := _ACTIVE.get()) is None:
        raise NotConnected(
            "no active Store — call eventic.connect(url) or use `with Store(url):`"
        )
    return s


def connect(url: str, *, create_tables: bool = True) -> Store:
    """Dev sugar: build a ``Store``, create tables, activate it, return it.

    ``Store(url)`` itself defaults ``create_tables=False`` — the production
    constructor does not silently DDL your database; the dev one-liner still
    works (F23).
    """
    return Store(url, create_tables=create_tables).activate()
