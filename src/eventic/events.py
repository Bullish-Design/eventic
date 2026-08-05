"""Event core: ``Event`` + the ``on_commit`` handler registry (I7).

Handlers fire *after* the version row is durable — exactly once per commit —
keyed by the **class object** (so same-named classes never cross-fire), in
MRO order, registration order within a class. Delivery backends are selected
per handler by ``mode``: ``"sync"`` (in-process, plugins/delivery.py) or
``"durable"`` (opt-in, eventic/dbos). A durable handler must name its queue
and the backend must be registered at registration time — both loud, never
silent.

[Deviation D5: handlers receive the ``Event`` (``event.record`` / ``event.kind``
/ ``event.delta``), not just ``event.record`` as the guide sketch's
``fn(event.record)`` implies — the delta contract ("update handler receives
the delta") requires it.]

"""

from __future__ import annotations

import itertools
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable

from .errors import EventicError


@dataclass(frozen=True)
class Event:
    """The fact that a version was committed — one per commit (I7)."""

    kind: str  # "create" | "update"
    record: Any  # the NEW version (Record subclass)
    delta: dict | None = None  # field-level changes (updates only)


# class object -> [(kind, fn, mode, queue, handler_id)] in registration order
_HANDLERS: dict[type, list[tuple[str, Callable, str, str | None, str]]] = defaultdict(list)
# stable per-function id (module:qualname) so durable dispatch survives restarts
_HANDLER_IDS: dict[str, Callable] = {}


def on_commit(*classes: type, kind: str = "*", mode: str = "sync", queue: str | None = None):
    """Register a post-commit handler for one or more Record classes.

    ``kind="*"`` fires for every event kind; ``kind="create"``/``"update"`
    restrict it. ``mode`` selects the delivery backend: ``"sync"`` (default,
    in-process, post-commit) or ``"durable"`` (opt-in DBOS queue — then
    ``queue`` names the explicit queue, and the handler receives the **id**,
    not the record). Failures in sync handlers are isolated and logged.
    """

    if mode != "sync":
        from .plugins import _DELIVERY_MODES  # lazy: avoid the import cycle

        if mode not in _DELIVERY_MODES:
            raise EventicError(
                f"delivery mode {mode!r} is not registered — import its backend "
                f"first (e.g. 'from eventic.dbos import DurableEvents' for the "
                f"durable mode)"
            )
    if mode == "durable" and not queue:
        raise EventicError(
            "durable handlers must name an explicit queue: "
            "on_commit(cls, mode='durable', queue='my-queue')"
        )

    def deco(fn: Callable) -> Callable:
        h_id = f"{fn.__module__}:{fn.__qualname__}"
        entry = (kind, fn, mode, queue, h_id)
        for c in classes:
            if entry not in _HANDLERS[c]:
                _HANDLERS[c].append(entry)
        _HANDLER_IDS.setdefault(h_id, fn)
        return fn

    return deco


def _reset_handlers() -> None:
    """Test hook: clear the registry so tests start with a clean slate."""
    _HANDLERS.clear()
    _HANDLER_IDS.clear()
