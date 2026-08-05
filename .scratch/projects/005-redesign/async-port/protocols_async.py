"""Paper port: the AsyncStore protocol (signatures only).

This file is discarded after the audit; it exists to prove the port touches
nothing above protocols.py. ~60 lines of signature duplication.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from eventic.envelopes import Page
from eventic.ids import AggregateKey
from eventic.jsonx import JsonValue
from eventic.protocols import Capabilities
from eventic.wire import (
    ClaimedIntent,
    CommitRequest,
    CommitResult,
    Settlement,
    StoredRevision,
)


class AsyncStore(Protocol):
    """The async twin of ``Store``. One request in, one awaitable out."""

    @property
    def capabilities(self) -> Capabilities: ...

    async def commit(self, requests: Sequence[CommitRequest]) -> Sequence[CommitResult]: ...

    async def head(self, key: AggregateKey) -> StoredRevision | None: ...

    async def revision(self, key: AggregateKey, revision: int) -> StoredRevision | None: ...

    async def history(
        self, key: AggregateKey, *, after: int, limit: int
    ) -> Page[StoredRevision]: ...

    async def search(
        self,
        stream: str,
        filters: Mapping[str, JsonValue],
        *,
        cursor: str | None,
        limit: int,
    ) -> Page[StoredRevision]: ...

    async def claim(
        self, queue: str, *, limit: int, lease: timedelta
    ) -> Sequence[ClaimedIntent]: ...

    async def settle(self, settlements: Sequence[Settlement]) -> None: ...
