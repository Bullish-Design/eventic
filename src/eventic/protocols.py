"""The store contract: seven methods, one request in, one value out.

No SQLAlchemy import, no ``Session`` parameter, no generator or iterator
return. This file is the seam the async port happens below.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from eventic.app import App
from eventic.envelopes import Page
from eventic.ids import AggregateKey
from eventic.jsonx import JsonValue
from eventic.wire import (
    ClaimedIntent,
    CommitRequest,
    CommitResult,
    Settlement,
    StoredRevision,
)


@dataclass(frozen=True)
class Capabilities:
    """Behavior the conformance suite tests, not marker attributes."""

    outbox: bool = False
    json_paths: bool = False
    concurrent_drainers: bool = False
    max_batch: int = 100


@dataclass(frozen=True)
class SchemaReport:
    """Result of ``eventic schema check``: fingerprint drift per stream.

    ``streams`` rows are ``(name, version, declared, stored, ok)`` where
    ``stored`` is ``None`` when no baseline was ever recorded and ``ok`` is
    then ``None`` too (a third state, distinct from clean and from drift).
    """

    streams: tuple[
        tuple[str, int, str, str | None, bool | None], ...
    ] = ()  # (name, version, declared, stored, ok)
    drift: bool = False
    baseline_missing: bool = False


@dataclass(frozen=True)
class RebuildReport:
    """Result of ``heads rebuild``."""

    streams: tuple[str, ...] = ()
    rebuilt: int = 0
    orphans_removed: int = 0
    mismatches: int = 0


@dataclass(frozen=True)
class VerifyReport:
    """Result of ``eventic verify``: digest equality across the log."""

    streams: tuple[str, ...] = ()
    revisions_checked: int = 0
    mismatches: int = 0


class Store(Protocol):
    """A backend that implements atomic commit and exact reads."""

    @property
    def capabilities(self) -> Capabilities: ...

    def commit(self, requests: Sequence[CommitRequest]) -> Sequence[CommitResult]: ...

    def head(self, key: AggregateKey) -> StoredRevision | None: ...

    def revision(self, key: AggregateKey, revision: int) -> StoredRevision | None: ...

    def history(
        self, key: AggregateKey, *, after: int, limit: int
    ) -> Page[StoredRevision]: ...

    def search(
        self,
        stream: str,
        filters: Mapping[str, JsonValue],
        *,
        cursor: str | None,
        limit: int,
    ) -> Page[StoredRevision]: ...

    def claim(
        self, queue: str, *, limit: int, lease: timedelta
    ) -> Sequence[ClaimedIntent]: ...

    def settle(self, settlements: Sequence[Settlement]) -> None: ...


class StoreAdmin(Protocol):
    """CLI-only operations; sync forever (R10)."""

    def migrate(self) -> None: ...

    def check(self, app: App) -> SchemaReport: ...

    def rebuild_heads(self, stream: str | None, *, chunk: int) -> RebuildReport: ...

    def verify(self, stream: str | None, *, chunk: int) -> VerifyReport: ...
