"""Subscriptions: ``on_commit`` + the per-class registry (CONCEPT §7.4, F10).

Delivery is a property of the **subscription**, never of the record class:
``via="inline"`` (in-process, post-commit) or ``via="outbox"`` (durable, via
the outbox — must name a queue). Subscriptions live on the class that declared
them and inherit through the MRO; ``subscriptions_for`` walks it, so there is
no process-global delivery registry and no cross-test leakage.

``_HANDLERS`` maps a stable per-function id (``module:qualname``) to the
function so durable dispatch survives restarts — and a second function
claiming the same id is a **loud** ``HandlerCollision`` (F22), not first-wins.
It is declaration state, written by code loading, never by operations (I8).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .errors import ConfigError, HandlerCollision

# handler id -> function, written by @on_commit (declaration, not state; I8)
_HANDLERS: dict[str, Callable] = {}


@dataclass(frozen=True, slots=True)
class Subscription:
    kind: str
    fn: Callable
    via: str
    queue: str | None
    handler_id: str

    def matches(self, kind: str) -> bool:
        return self.kind in ("*", kind)


def on_commit(*classes: type, kind: str = "*", via: str = "inline", queue: str | None = None):
    """Register a post-commit subscription for one or more Record classes.

    ``kind="*"`` fires for every event kind; ``"create"``/``"update"``
    restrict it. ``via`` selects delivery: ``"inline"`` (default, in-process,
    post-commit) or ``"outbox"`` (durable — then ``queue`` is required and the
    row is staged inside the commit transaction). Failures are isolated and
    logged, never propagated to the writer.
    """
    if via not in ("inline", "outbox"):
        raise ConfigError(f"unknown delivery {via!r}")
    if via == "outbox" and not queue:
        raise ConfigError("outbox subscriptions must name a queue")

    def deco(fn: Callable) -> Callable:
        hid = f"{fn.__module__}:{fn.__qualname__}"
        if _HANDLERS.setdefault(hid, fn) is not fn:
            raise HandlerCollision(
                f"handler id {hid!r} is already registered by {_HANDLERS[hid]}"
            )
        sub = Subscription(kind, fn, via, queue, hid)
        for c in classes:
            c.__dict__["__subscriptions__"].append(sub)
        return fn

    return deco


def subscriptions_for(cls: type) -> list[Subscription]:
    """Every subscription on ``cls``'s MRO, base→derived, registration order."""
    return [
        s
        for c in cls.__mro__
        for s in c.__dict__.get("__subscriptions__", ())
    ]
