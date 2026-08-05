"""Event core: ``Event`` + the ``on_commit`` handler registry (I7).

Handlers fire *after* the version row is durable — exactly once per commit —
keyed by the **class object** (so same-named classes never cross-fire), in
MRO order, registration order within a class. The default ``sync`` backend
lives in ``plugins/delivery.py``; a durable backend is opt-in (``eventic[dbos]``).

[Deviation D5: handlers receive the ``Event`` (``event.record`` / ``event.kind``
/ ``event.delta``), not just ``event.record`` as the guide sketch's
``fn(event.record)`` implies — the delta contract ("update handler receives
the delta") requires it.]

NOTE (deviation D1): the old 0.1 ``events.py`` keeps its module path until the
Phase-6 swap; this module carries the *new* event core under the working name
``eventbus.py`` and is renamed to ``events.py`` at Step 12.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Event:
    """The fact that a version was committed — one per commit (I7)."""

    kind: str  # "create" | "update"
    record: Any  # the NEW version (Record subclass)
    delta: dict | None = None  # field-level changes (updates only)


# class object -> [(kind, handler, mode)] in registration order
_HANDLERS: dict[type, list[tuple[str, Callable, str]]] = defaultdict(list)


def on_commit(*classes: type, kind: str = "*", mode: str = "sync"):
    """Register a post-commit handler for one or more Record classes.

    ``kind="*"`` fires for every event kind; ``kind="create"``/``kind="update"`
    restrict it. ``mode`` selects the delivery backend (``"sync"`` default;
    ``"durable"`` via the opt-in DBOS plugin).
    """

    def deco(fn: Callable) -> Callable:
        for c in classes:
            _HANDLERS[c].append((kind, fn, mode))
        return fn

    return deco


def _reset_handlers() -> None:
    """Test hook: clear the registry so tests start with a clean slate."""
    _HANDLERS.clear()
