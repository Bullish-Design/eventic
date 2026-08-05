"""Paper port: the async store skeleton (NotImplementedError bodies).

Confirms ``sql/statements.py`` is reused verbatim — every body below is
``execute() -> await execute()`` glue. ~150 lines. Discarded after the audit.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any
from uuid import UUID

from eventic.envelopes import Page
from eventic.ids import AggregateKey
from eventic.jsonx import JsonValue
from eventic.protocols import Capabilities
from eventic.sql import statements as st  # reused verbatim
from eventic.wire import ClaimedIntent, CommitRequest, CommitResult, Settlement, StoredRevision


class AsyncSqlStore:
    """The async twin: identical algorithm, ``await conn.execute`` glue."""

    async def commit(self, requests: Sequence[CommitRequest]) -> Sequence[CommitResult]:
        raise NotImplementedError

    async def head(self, key: AggregateKey) -> StoredRevision | None:
        raise NotImplementedError

    async def revision(self, key: AggregateKey, revision: int) -> StoredRevision | None:
        raise NotImplementedError

    async def history(
        self, key: AggregateKey, *, after: int, limit: int
    ) -> Page[StoredRevision]:
        raise NotImplementedError

    async def search(
        self,
        stream: str,
        filters: Mapping[str, JsonValue],
        *,
        cursor: str | None,
        limit: int,
    ) -> Page[StoredRevision]:
        raise NotImplementedError

    async def claim(
        self, queue: str, *, limit: int, lease: timedelta
    ) -> Sequence[ClaimedIntent]:
        raise NotImplementedError

    async def settle(self, settlements: Sequence[Settlement]) -> None:
        raise NotImplementedError
