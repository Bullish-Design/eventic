"""``SqlStore`` — the default ``RowStore`` (log + head + outbox on one DB).

Stateless: every method receives a ``Session`` from the caller (the unit of
work for writes, a read session for reads). That is what lets a class declare
its store at definition time without coupling to a connection.

``append`` upholds I1 (append-only), I4 (deterministic version_id) and I5
(loud conflicts): an ``IntegrityError`` on the ``(id, version)`` pair is a
*byte-identical replay* (same version_id **and** same data) only when it is a
silent no-op; anything else is a different writer and raises
``StaleVersionError``. The check-then-insert shape means the only
``IntegrityError`` left is a genuinely lost race, which is loud by definition.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Mapping

from sqlalchemy import cast, func, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..errors import RecordNotFound, StaleVersionError
from ..event import Event
from ..seams import Window
from ..subscribe import Subscription
from .schema import HeadRow, LogRow, OutboxRow, now_utc


def _jsonable(value: Any) -> Any:
    """Normalize filter values to what the JSON column actually stores."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


class SqlStore:
    """The null ``RowStore``: the full triad on one database."""

    json_documents = True  # satisfies the JsonRowStore marker (Delta's requires)

    # ------------------------------------------------------------------ #
    # write
    # ------------------------------------------------------------------ #
    def append(self, s: Session, row: LogRow) -> bool:
        """Insert one immutable log row. Replay → False; conflict → loud."""
        existing = s.execute(
            select(LogRow.version_id, LogRow.data).where(
                LogRow.id == row.id, LogRow.version == row.version
            )
        ).one_or_none()
        if existing is not None:
            self._decide(existing, row)
            return False
        try:
            s.add(row)
            s.flush()
        except IntegrityError:
            # lost a race the pre-check could not see -> a different writer
            raise StaleVersionError(row.id, row.version) from None
        return True

    def _decide(self, existing, row: LogRow) -> None:
        """I5: byte-identical replay is a silent no-op; anything else is loud."""
        if existing.version_id == row.version_id and existing.data == row.data:
            return
        raise StaleVersionError(row.id, row.version)

    def upsert_head(self, s: Session, head: HeadRow, *, force: bool = False) -> None:
        """Write/replace the head row; out-of-order writes are ignored unless
        ``force`` (used by rebuild-heads, which must be able to repair)."""
        existing = s.execute(
            select(HeadRow.version).where(
                HeadRow.stream == head.stream, HeadRow.id == head.id
            )
        ).scalar()
        if existing is None:
            s.add(head)
        elif force or head.version > existing:
            s.execute(
                update(HeadRow)
                .where(HeadRow.stream == head.stream, HeadRow.id == head.id)
                .values(
                    version=head.version,
                    version_id=head.version_id,
                    committed_at=head.committed_at,
                    state=head.state,
                )
            )

    def stage_outbox(self, s: Session, sub: Subscription, event: Event) -> None:
        """Record one pending durable delivery inside the same transaction."""
        rec = event.record
        s.add(
            OutboxRow(
                version_id=rec.version_id,
                stream=type(rec).__eventic__.stream,
                record_id=rec.id,
                version=rec.version,
                kind=event.kind,
                delta=event.delta,
                handler_id=sub.handler_id,
                queue=sub.queue,
            )
        )

    # ------------------------------------------------------------------ #
    # reads
    # ------------------------------------------------------------------ #
    def read(
        self,
        s: Session,
        stream: str,
        rec_id: uuid.UUID,
        window: Window,
        version: int,
    ) -> list[LogRow]:
        """Log rows for an exact version, bounded by the codec's window."""
        base = select(LogRow).where(
            LogRow.stream == stream, LogRow.id == rec_id, LogRow.version <= version
        )
        if window is Window.POINT:
            stmt = base.where(LogRow.version == version)
        else:  # SINCE_SNAPSHOT: back to the nearest snapshot (≤ K rows, F17)
            latest_snapshot = (
                select(func.max(LogRow.version))
                .where(
                    LogRow.stream == stream,
                    LogRow.id == rec_id,
                    LogRow.snapshot.is_(True),
                    LogRow.version <= version,
                )
                .scalar_subquery()
            )
            stmt = base.where(LogRow.version >= latest_snapshot)
        rows = list(s.execute(stmt.order_by(LogRow.version)).scalars())
        if rows and rows[-1].version == version:
            return rows
        return []

    def stream(self, s: Session, stream: str, rec_id: uuid.UUID) -> list[LogRow]:
        """All log rows for an aggregate, oldest→newest (the log, I1)."""
        return list(
            s.execute(
                select(LogRow)
                .where(LogRow.stream == stream, LogRow.id == rec_id)
                .order_by(LogRow.version)
            ).scalars()
        )

    def all_rows(self, s: Session, stream: str) -> list[LogRow]:
        """Every log row of a stream (head rebuilds)."""
        return list(
            s.execute(
                select(LogRow)
                .where(LogRow.stream == stream)
                .order_by(LogRow.id, LogRow.version)
            ).scalars()
        )

    def head(self, s: Session, stream: str, rec_id: uuid.UUID) -> HeadRow | None:
        """The derived head row, or None."""
        return s.execute(
            select(HeadRow).where(HeadRow.stream == stream, HeadRow.id == rec_id)
        ).scalars().first()

    def search(self, s: Session, stream: str, eq: Mapping[str, Any]) -> list[HeadRow]:
        """Head rows matching every equality filter, with real pushdown (F16).

        Postgres: one ``state @> :json`` containment against the GIN index
        (dotted keys become nested JSON). SQLite: AND-ed ``json_extract``
        comparisons. Equality only — build a predicate AST when a second
        predicate kind arrives (CONCEPT §8.1).
        """
        stmt = select(HeadRow).where(HeadRow.stream == stream)
        if not eq:
            return list(s.execute(stmt).scalars())
        dialect = s.bind.dialect.name if s.bind is not None else None
        if dialect == "postgresql":
            nested: dict = {}
            for key, value in eq.items():
                cur = nested
                parts = key.split(".")
                for p in parts[:-1]:
                    cur = cur.setdefault(p, {})
                cur[parts[-1]] = value
            stmt = stmt.where(
                cast(HeadRow.state, postgresql.JSONB).op("@>")(
                    cast(nested, postgresql.JSONB)
                )
            )
        else:
            for key, value in eq.items():
                stmt = stmt.where(func.json_extract(HeadRow.state, f"$.{key}") == value)
        return list(s.execute(stmt).scalars())
