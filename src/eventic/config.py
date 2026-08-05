"""``EventicConfig`` — keyword resolution and the stream registry (F1/F2/F13).

Seams are selected by **class keyword** (``stream=``, ``rows=``, ``codec=``,
``interceptors=``). Config resolves through the MRO like any other class
attribute, so subclassing works the way Python users expect — framework types
never enter the pydantic MRO (v2's phantom-field bug). ``stream`` is **not**
inherited: one stream per concrete class, and a collision is loud (F13).

``_STREAMS`` maps stream name → class. It is written by ``__init_subclass__``
at code-loading time only — declaration, not state (I8, CONCEPT §3.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .codec.snapshot import Snapshot
from .errors import ConfigError, SeamMismatch, StreamCollision
from .seams import RowStore
from .store.sql import SqlStore

# handler id registry lives in subscribe.py; the stream registry here.
_STREAMS: dict[str, type] = {}


@dataclass(frozen=True, slots=True)
class EventicConfig:
    stream: str
    rows: RowStore
    codec: Any
    interceptors: tuple = ()


DEFAULT_ROWS: RowStore = SqlStore()  # stateless; safe to share
DEFAULT_CODEC = Snapshot()
DEFAULT_CONFIG = EventicConfig(
    stream="<Record>", rows=DEFAULT_ROWS, codec=DEFAULT_CODEC, interceptors=()
)


def register_stream(name: str, cls: type) -> None:
    """Claim a stream name. A second class claiming it is loud (F13)."""
    prior = _STREAMS.get(name)
    if prior is not None and prior is not cls:
        raise StreamCollision(
            f"stream {name!r} is already claimed by "
            f"{prior.__module__}.{prior.__qualname__}; give "
            f"{cls.__qualname__} an explicit stream=... (one stream, one class)"
        )
    _STREAMS[name] = cls


def stream_class(name: str) -> type:
    """The Record class for a stream (outbox dispatch, rebuild-heads)."""
    cls = _STREAMS.get(name)
    if cls is None:
        raise ConfigError(f"no Record class registered for stream {name!r}")
    return cls


def resolve_config(
    cls: type,
    *,
    stream: str | None,
    rows: Any,
    codec: Any,
    interceptors: Any,
) -> EventicConfig:
    """Build the config for a new class, resolving through the MRO."""
    inherited: EventicConfig = getattr(cls, "__eventic__", DEFAULT_CONFIG)
    cfg = EventicConfig(
        stream=stream or cls.__name__,  # NOT inherited: one stream per class
        rows=rows if rows is not None else inherited.rows,
        codec=codec if codec is not None else inherited.codec,
        interceptors=tuple(interceptors) if interceptors is not None else inherited.interceptors,
    )
    # The capability check, as a type (F12): loud AT CLASS DEFINITION.
    requires = getattr(cfg.codec, "requires", RowStore)
    if not isinstance(cfg.rows, requires):
        raise SeamMismatch(
            f"{cls.__name__}: {type(cfg.codec).__name__} requires "
            f"{requires.__name__}, but {type(cfg.rows).__name__} does not provide it"
        )
    register_stream(cfg.stream, cls)
    return cfg
