"""Pure SQL statement builders. Nothing here executes (R4).

Every function returns a SQLAlchemy Core construct built through the dialect.
``sql/store.py`` is the only executor.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select, update

from eventic.sql.dialect import Dialect
from eventic.sql.tables import (
    eventic_head as heads,
)
from eventic.sql.tables import (
    eventic_intent as intents,
)
from eventic.sql.tables import (
    eventic_revision as revisions,
)
from eventic.sql.tables import (
    eventic_schema as schema,
)
from eventic.wire import Settlement


def select_head(stream: str, aggregate_id: UUID, *, for_update: bool) -> Any:
    stmt = select(heads).where(
        heads.c.stream == stream,
        heads.c.aggregate_id == aggregate_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    return stmt


def select_revision_row(stream: str, aggregate_id: UUID, revision: int) -> Any:
    return select(revisions).where(
        revisions.c.stream == stream,
        revisions.c.aggregate_id == aggregate_id,
        revisions.c.revision == revision,
    )


def select_window(stream: str, aggregate_id: UUID, start: int, end: int) -> Any:
    """One range query over the log, inclusive, in revision order."""
    return (
        select(revisions)
        .where(
            revisions.c.stream == stream,
            revisions.c.aggregate_id == aggregate_id,
            revisions.c.revision >= start,
            revisions.c.revision <= end,
        )
        .order_by(revisions.c.revision)
    )


def select_history(stream: str, aggregate_id: UUID, *, since: int, limit: int) -> Any:
    return (
        select(revisions)
        .where(
            revisions.c.stream == stream,
            revisions.c.aggregate_id == aggregate_id,
            revisions.c.revision > since,
        )
        .order_by(revisions.c.revision)
        .limit(limit)
    )


def select_latest_revision(stream: str, aggregate_id: UUID) -> Any:
    return (
        select(revisions)
        .where(
            revisions.c.stream == stream,
            revisions.c.aggregate_id == aggregate_id,
        )
        .order_by(revisions.c.revision.desc())
        .limit(1)
    )


def insert_revision(dialect: Dialect, values: dict[str, Any]) -> Any:
    return revisions.insert().values(**values)


def upsert_head(dialect: Dialect, values: dict[str, Any]) -> Any:
    return dialect.upsert_head(values)


def insert_intents(dialect: Dialect, values: list[dict[str, Any]]) -> Any:
    return intents.insert().values(values)


def claim_intents(
    dialect: Dialect,
    queue: str,
    now: Any,
    lease_until: Any,
    limit: int,
) -> Any:
    """Lease the oldest claimable intents and return them in one statement."""
    inner = dialect.claim_select(queue, now, limit)
    claimed = (
        update(intents)
        .where(intents.c.intent_id.in_(inner))
        .values(
            status="leased",
            leased_until=lease_until,
            attempts=intents.c.attempts + 1,
        )
        .returning(
            intents.c.intent_id,
            intents.c.subscription_id,
            intents.c.revision_id,
            intents.c.queue,
            intents.c.attempts,
        )
    )
    return claimed


def settle_intents(dialect: Dialect, settlements: Sequence[Settlement]) -> list[Any]:
    """One statement per settlement: delete on delivery, update otherwise."""
    statements: list[Any] = []
    for settlement in settlements:
        if settlement.status == "delivered":
            statements.append(
                delete(intents).where(intents.c.intent_id == settlement.intent_id)
            )
        elif settlement.status == "retry":
            statements.append(
                update(intents)
                .where(intents.c.intent_id == settlement.intent_id)
                .values(
                    status="pending",
                    leased_until=None,
                    available_at=settlement.available_at,
                    last_error=settlement.error,
                )
            )
        else:  # dead
            statements.append(
                update(intents)
                .where(intents.c.intent_id == settlement.intent_id)
                .values(
                    status="dead",
                    leased_until=None,
                    last_error=settlement.error,
                )
            )
    return statements


def search_heads(
    dialect: Dialect,
    stream: str,
    filters: Any,
    *,
    cursor: str | None,
    limit: int,
) -> Any:
    stmt = select(heads).where(heads.c.stream == stream)
    for path, value in filters.items():
        stmt = stmt.where(dialect.path_equals(heads.c.state, path, value))
    if cursor is not None:
        stmt = stmt.where(heads.c.aggregate_id > cursor)
    return stmt.order_by(heads.c.aggregate_id).limit(limit)


def upsert_fingerprint(dialect: Dialect, values: dict[str, Any]) -> Any:
    return dialect.upsert_fingerprint(values)


def select_fingerprint(stream: str, schema_version: int) -> Any:
    return select(schema).where(
        schema.c.stream == stream,
        schema.c.schema_version == schema_version,
    )


def select_all_heads_for(stream: str | None) -> Any:
    stmt = select(heads)
    if stream is not None:
        stmt = stmt.where(heads.c.stream == stream)
    return stmt.order_by(heads.c.stream, heads.c.aggregate_id)


def select_all_log_for(stream: str | None, *, chunk: int, offset: int) -> Any:
    stmt = select(revisions).order_by(
        revisions.c.stream, revisions.c.aggregate_id, revisions.c.revision
    )
    if stream is not None:
        stmt = stmt.where(revisions.c.stream == stream)
    return stmt.offset(offset).limit(chunk)


def delete_heads(stream: str | None) -> Any:
    stmt = delete(heads)
    if stream is not None:
        stmt = stmt.where(heads.c.stream == stream)
    return stmt
