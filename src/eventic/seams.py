"""The three protocol seams (CONCEPT §7).

Seams are selected by **class keyword**, never by inheritance (F1/F2), and
each is a ``Protocol`` — not a registry entry, not a capability token. The
capability-token DSL is replaced by Python's type system: ``Delta`` declares
``requires = JsonRowStore`` and the check runs at class definition.

- ``RowStore`` — where and how rows are stored and queried. Exclusive.
  Implementations are **stateless**: they receive a ``Session`` rather than
  holding an engine, which is what lets a class declare its store at
  definition time without coupling to a connection.
- ``JsonRowStore`` — marker: ``data`` is an opaque JSON document. ``Delta``
  requires this (F12's ``provides``/``requires`` vocabulary, expressed as a
  type).
- ``Codec`` — how a version's state becomes a row's ``data``. Exclusive.
- ``Interceptor`` — ``before_commit`` (may veto) / ``after_commit`` /
  ``after_hydrate``. Stacking.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING, Any, ClassVar, Iterator, Mapping, Protocol, Sequence, runtime_checkable

if TYPE_CHECKING:
    from .store.schema import HeadRow, LogRow
    from .subscribe import Subscription
    from .event import Event


class Window(enum.Enum):
    """How much of the log a codec needs to answer a point read (F17)."""

    POINT = "point"  # exactly the target row
    SINCE_SNAPSHOT = "since_snapshot"  # the window back to the nearest snapshot


@runtime_checkable
class RowStore(Protocol):
    """The persistence seam — how & where rows are stored and queried."""

    def append(self, s, row: LogRow) -> bool:
        """Insert one immutable log row; replay → False, conflict → raise
        ``StaleVersionError`` (I5)."""

    def read(self, s, stream: str, rec_id, window: Window, version: int) -> list[LogRow]:
        """Log rows for an exact version (bounded by the codec's window)."""

    def stream(self, s, stream: str, rec_id) -> list[LogRow]:
        """All log rows for an aggregate, oldest→newest (history)."""

    def all_rows(self, s, stream: str) -> list[LogRow]:
        """Every log row of a stream (head rebuilds)."""

    def head(self, s, stream: str, rec_id) -> HeadRow | None:
        """The derived head row, or None."""

    def upsert_head(self, s, head: HeadRow) -> None:
        """Write/replace the head row; out-of-order writes are ignored."""

    def search(self, s, stream: str, eq: Mapping[str, Any]) -> list[HeadRow]:
        """Head rows matching every equality filter (with SQL pushdown)."""

    def stage_outbox(self, s, sub: Subscription, event: Event) -> None:
        """Record one pending durable delivery inside the same transaction."""


@runtime_checkable
class JsonRowStore(RowStore, Protocol):
    """Marker: ``data`` is an opaque JSON document. ``Delta`` requires this.

    The marker attribute is what makes the seam check a *type* (F12): a store
    that does not store JSON documents cannot accidentally satisfy the
    protocol, because runtime-checkable ``isinstance`` requires the marker.
    """

    json_documents: bool = True


class Codec(Protocol):
    """How a version's state becomes a row's ``data`` (exclusive seam)."""

    requires: ClassVar[type] = RowStore

    def encode(self, prev, new) -> tuple[dict, bool]:
        """(data, snapshot). A snapshot row's data is the full user state."""

    def decode(self, rows: Sequence[LogRow]) -> dict:
        """Reconstruct the user state at the last row."""

    def window(self) -> Window:
        """How far back a point read must reach (F17)."""

    def iter_states(self, rows: Sequence[LogRow]) -> Iterator[tuple[dict, LogRow]]:
        """(state, row) for every row — a single forward fold (F19)."""


class Interceptor(Protocol):
    def before_commit(self, record):
        """Inspect/enrich the pending version, or raise ``Veto`` to abort.
        The return value IS threaded (F11)."""

    def after_commit(self, event) -> None:
        """Runs only once the version is durable; isolated."""

    def after_hydrate(self, record):
        """Transform a freshly reconstructed object (decrypt, redact)."""
