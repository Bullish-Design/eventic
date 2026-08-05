"""``UnitOfWork`` — the durability line (CONCEPT §4, F3).

**A commit is not an append. A commit is a transaction that contains an
append.** The pipeline never emits; it stages ``Event`` objects on the unit of
work, and the unit of work flushes them only after the transaction has
actually committed.

- An owning UoW (the store's own session) commits in ``__exit__`` — **the
  durability line** — then flushes.
- A non-owning UoW (a DBOS workflow's transaction, or any caller's session)
  binds to that session's ``after_commit`` / ``after_rollback`` events instead.
  One mechanism, both paths.
- A byte-identical replay inserts nothing, therefore stages nothing, therefore
  emits nothing: "exactly once per commit" falls out structurally.

Nesting: a UoW opened inside an existing one is a proxy that stages into the
parent and never commits, so an inner write inside an outer transaction emits
once, with the outer.
"""

from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING

from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from ..event import Event

_CURRENT: contextvars.ContextVar["UnitOfWork | None"] = contextvars.ContextVar(
    "eventic_uow", default=None
)


class UnitOfWork:
    """The transaction boundary plus its staged events."""

    def __init__(self, session: Session, *, owns_commit: bool):
        self.session = session
        self._owns = owns_commit
        self._staged: list[Event] = []

    @classmethod
    def current(cls) -> "UnitOfWork | None":
        return _CURRENT.get()

    def stage(self, event: "Event") -> None:
        self._staged.append(event)

    def __enter__(self) -> "UnitOfWork":
        self._token = _CURRENT.set(self)
        if not self._owns:
            # We do NOT control COMMIT. Bind to the owner's signal instead, so
            # the durability line holds identically on both paths.
            sa_event.listen(self.session, "after_commit", self._flush, once=True)
            sa_event.listen(self.session, "after_rollback", self._discard, once=True)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        _CURRENT.reset(self._token)
        if not self._owns:
            return False  # the owner commits; our listener flushes
        try:
            if exc_type is not None:
                self.session.rollback()
                self._staged.clear()
                return False
            self.session.commit()  # ◄── THE DURABILITY LINE
            self._flush()
        finally:
            self.session.close()  # we own it; never leak it
        return False

    def _flush(self, *_) -> None:
        from ..dispatch.inline import dispatch_inline  # lazy: avoid cycles

        staged, self._staged = self._staged, []
        for event in staged:
            dispatch_inline(event)  # isolated per handler; never propagates

    def _discard(self, *_) -> None:
        self._staged.clear()


class _Nested:
    """A UoW inside an existing one: stage into the parent, never commit."""

    def __init__(self, parent: UnitOfWork):
        self._parent = parent

    @property
    def session(self) -> Session:
        return self._parent.session

    def stage(self, event: "Event") -> None:
        self._parent.stage(event)

    def __enter__(self) -> "_Nested":
        return self

    def __exit__(self, *exc) -> bool:
        return False
