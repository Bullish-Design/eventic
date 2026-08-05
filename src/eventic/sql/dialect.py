"""Per-dialect behavior: JSON access, upsert, locking, capabilities.

A ``Dialect`` is a frozen value object. ``statements.py`` builds SQLAlchemy
Core constructs through it; ``store.py`` executes them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, Insert, and_, or_
from sqlalchemy import select as sa_select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from eventic.jsonx import JsonValue
from eventic.protocols import Capabilities
from eventic.sql.tables import (
    eventic_head as eventic_head_table,
)
from eventic.sql.tables import (
    eventic_intent as eventic_intent_table,
)
from eventic.sql.tables import (
    eventic_schema as eventic_schema_table,
)


def split_path(path: str) -> list[str]:
    """Split a dotted filter path, honoring ``\\`` escaping for literal dots."""
    segments: list[str] = []
    current: list[str] = []
    escaped = False
    for char in path:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ".":
            segments.append("".join(current))
            current = []
        else:
            current.append(char)
    if escaped:
        current.append("\\")
    segments.append("".join(current))
    return segments


def _json_path_string(path: str) -> str:
    parts: list[str] = []
    for segment in split_path(path):
        quoted: str = segment.replace('"', '""')
        parts.append(f'."{quoted}"')
    return "$" + "".join(parts)


def _nested(path: str, value: JsonValue) -> dict[str, Any]:
    """A nested dict with ``value`` at the end of ``path``, for Postgres ``@>``."""
    tree: dict[str, Any] = {}
    node = tree
    segments = split_path(path)
    for segment in segments[:-1]:
        node = node.setdefault(segment, {})
    node[segments[-1]] = json.loads(json.dumps(value, ensure_ascii=False))
    return tree


@dataclass(frozen=True)
class Dialect:
    """Behavioral differences between the two supported backends."""

    name: str  # "sqlite" | "postgresql"
    capabilities: Capabilities

    def path_equals(
        self, column: ColumnElement[Any], path: str, value: JsonValue
    ) -> ColumnElement[Any]:
        """Equality on a dotted path, distinguishing missing from JSON null."""
        if self.name == "sqlite":
            from sqlalchemy import func

            path_str = _json_path_string(path)
            if value is None:
                return and_(
                    func.json_type(column, path_str).isnot(None),
                    func.json_extract(column, path_str).is_(None),
                )
            if isinstance(value, bool):
                return and_(
                    func.json_type(column, path_str) == ("true" if value else "false"),
                    func.json_extract(column, path_str) == (1 if value else 0),
                )
            if isinstance(value, str):
                return and_(
                    func.json_type(column, path_str) == "text",
                    func.json_extract(column, path_str) == value,
                )
            if isinstance(value, float):
                return and_(
                    func.json_type(column, path_str) == "real",
                    func.json_extract(column, path_str) == value,
                )
            return and_(
                func.json_type(column, path_str) == "integer",
                func.json_extract(column, path_str) == value,
            )
        # Postgres: explicit containment of the exact nested value. A JSON null
        # in the document is present; a missing path is not.
        return column.op("@>")(_nested(path, value))

    def upsert_head(self, values: dict[str, Any]) -> Insert:
        """Upsert an ``eventic_head`` row, replacing the whole row on conflict."""
        if self.name == "postgresql":
            insert = pg_insert(eventic_head_table).values(**values)
            excluded = insert.excluded
            return insert.on_conflict_do_update(
                index_elements=["stream", "aggregate_id"],
                set_={
                    "revision": excluded.revision,
                    "revision_id": excluded.revision_id,
                    "schema_version": excluded.schema_version,
                    "meta_version": excluded.meta_version,
                    "state": excluded.state,
                    "digest": excluded.digest,
                    "meta": excluded.meta,
                    "committed_at": excluded.committed_at,
                },
            )
        insert = sqlite_insert(eventic_head_table).values(**values)
        return insert.on_conflict_do_update(
            index_elements=["stream", "aggregate_id"],
            set_={
                "revision": insert.excluded.revision,
                "revision_id": insert.excluded.revision_id,
                "schema_version": insert.excluded.schema_version,
                "meta_version": insert.excluded.meta_version,
                "state": insert.excluded.state,
                "digest": insert.excluded.digest,
                "meta": insert.excluded.meta,
                "committed_at": insert.excluded.committed_at,
            },
        )

    def upsert_fingerprint(self, values: dict[str, Any]) -> Insert:
        """Insert a fingerprint row, preserving ``first_seen`` on conflict."""
        if self.name == "postgresql":
            insert = pg_insert(eventic_schema_table).values(**values)
            return insert.on_conflict_do_nothing(
                index_elements=["stream", "schema_version"]
            )
        insert = sqlite_insert(eventic_schema_table).values(**values)
        return insert.on_conflict_do_nothing(
            index_elements=["stream", "schema_version"]
        )

    def claim_select(self, queue: str, now: Any, limit: int) -> Any:
        """The inner SELECT of the claim statement, with the right locking."""
        intent = eventic_intent_table
        claimable = or_(
            and_(
                intent.c.status == "pending",
                intent.c.available_at <= now,
            ),
            and_(
                intent.c.status == "leased",
                intent.c.leased_until < now,
            ),
        )
        select = (
            sa_select(intent.c.intent_id)
            .where(intent.c.queue == queue, claimable)
            .order_by(intent.c.available_at)
            .limit(limit)
        )
        if self.name == "postgresql":
            select = select.with_for_update(skip_locked=True)
        return select


SQLITE_CAPABILITIES = Capabilities(
    outbox=True,
    json_paths=True,
    concurrent_drainers=False,
    max_batch=100,
)

POSTGRES_CAPABILITIES = Capabilities(
    outbox=True,
    json_paths=True,
    concurrent_drainers=True,
    max_batch=1000,
)
