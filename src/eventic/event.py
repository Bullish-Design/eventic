"""``Event`` — the fact that a version was committed (I7).

Not a separately stored object: one per commit, handed to sync handlers
post-durability and *referenced* by outbox rows so durable handlers receive
the same object shape. ``record`` is the new version; ``delta`` is the
field-level change (updates only).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Event:
    kind: str  # "create" | "update"
    record: Any  # the NEW version (a Record subclass instance)
    delta: dict | None = None  # field-level changes (updates only)
