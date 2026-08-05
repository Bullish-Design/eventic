"""eventic — versioned Pydantic aggregates whose history is an event stream.

Pure-Python core (pydantic + SQLAlchemy only, I6); delta storage, durable
delivery, and alternative stores are opt-in. Importing this package never
imports ``dbos`` or ``fastapi`` — the optional driver is always an explicit
import (``from eventic.contrib import dbos``).

The public surface (CONCEPT §8): the core is the smallest implementation that
upholds the invariants and runs the pipeline. Roughly a third of 0.2's surface
was speculative and is deleted outright (F8/F12).
"""

from .codec.delta import Delta
from .codec.snapshot import Snapshot
from .dispatch.outbox import OutboxDispatcher
from .errors import (
    ConfigError,
    EventicError,
    HandlerCollision,
    NotConnected,
    RecordNotFound,
    SeamMismatch,
    StaleVersionError,
    StreamCollision,
    UsageError,
    Veto,
)
from .identity import version_id
from .interceptors import Interceptor
from .record import Draft, Record
from .store import Store, active_store, connect
from .subscribe import on_commit

__version__ = "0.3.0"

__all__ = [
    "Record",
    "Draft",
    "connect",
    "Store",
    "active_store",
    "on_commit",
    "version_id",
    "Snapshot",
    "Delta",
    "Interceptor",
    "Veto",
    "OutboxDispatcher",
    "EventicError",
    "NotConnected",
    "RecordNotFound",
    "StaleVersionError",
    "StreamCollision",
    "HandlerCollision",
    "SeamMismatch",
    "ConfigError",
    "UsageError",
]
