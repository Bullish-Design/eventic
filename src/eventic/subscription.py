"""Subscriptions: declaration-time handlers with a delivery strategy."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from eventic.envelopes import Commit, Kind
from eventic.stream import Stream


@dataclass(frozen=True)
class Inline:
    """Best-effort, in-process delivery after ``COMMIT`` returns."""


@dataclass(frozen=True)
class Backoff:
    """Exponential backoff parameters for outbox retries."""

    max_attempts: int = 12
    base: float = 1.0
    factor: float = 2.0
    cap: float = 3600.0


@dataclass(frozen=True)
class Outbox:
    """Durable at-least-once delivery via a named queue."""

    queue: str = "default"
    retry: Backoff = Backoff()
    dead_letter: bool = True


@dataclass(frozen=True)
class Subscription[T: object, M: object]:
    """One handler, for one stream, for a set of kinds, with one delivery."""

    id: str
    stream: Stream[T]
    handler: Callable[[Commit[T, M]], None]
    kinds: frozenset[Kind] = field(
        default_factory=lambda: frozenset({"create", "change"})
    )
    delivery: Inline | Outbox = Inline()
