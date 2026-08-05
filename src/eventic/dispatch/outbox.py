"""The real outbox (Step 10, F10).

Outbox rows are written **inside the same transaction** as the log row, so
durable delivery is atomic by construction rather than by capability token.
An ``OutboxDispatcher`` claims ready rows, rebuilds the full ``Event`` (the
same object a sync handler gets — v2's bare-id signature existed only because
the outbox was fake), runs the handler, and deletes the row on success.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import stream_class
from ..errors import RecordNotFound
from ..event import Event
from ..pipeline import hydrate
from ..store import Store, active_store
from ..store.schema import OutboxRow, now_utc
from ..subscribe import _HANDLERS

logger = logging.getLogger(__name__)


def claim_ready(s: Session, *, queue: str | None = None, limit: int = 100) -> list[OutboxRow]:
    """Ready outbox rows, oldest first. Postgres: ``FOR UPDATE SKIP LOCKED``
    so concurrent drains never double-deliver; SQLite uses a plain SELECT."""
    stmt = (
        select(OutboxRow)
        .where(OutboxRow.available_at <= now_utc())
        .order_by(OutboxRow.seq)
        .limit(limit)
    )
    if queue is not None:
        stmt = stmt.where(OutboxRow.queue == queue)
    if s.bind is not None and s.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    return list(s.execute(stmt).scalars())


def _backoff(attempts: int) -> int:
    return min(3600, 2**attempts)


class OutboxDispatcher:
    """Run pending durable deliveries; delete on success, back off on failure."""

    def __init__(self, store: Store | None = None):
        self.store = store or active_store()

    def drain(self, *, queue: str | None = None, limit: int = 100) -> int:
        """Claim ready rows, rebuild each Event, run its handler."""
        with Session(self.store.engine, future=True) as s:
            rows = claim_ready(s, queue=queue, limit=limit)
            for row in rows:
                self._deliver(s, row)
            s.commit()
        return len(rows)

    def _deliver(self, s: Session, row: OutboxRow) -> None:
        try:
            _run_handler(s, row)
        except RecordNotFound:
            # the version no longer exists (log pruned) — nothing to deliver
            s.delete(row)
        except Exception:
            logger.exception("outbox row %s failed (attempt %s)", row.seq, row.attempts + 1)
            s.execute(
                update(OutboxRow)
                .where(OutboxRow.seq == row.seq)
                .values(
                    attempts=OutboxRow.attempts + 1,
                    available_at=now_utc() + timedelta(seconds=_backoff(row.attempts + 1)),
                )
            )
        else:
            s.delete(row)


def _run_handler(s: Session, row: OutboxRow) -> None:
    cls = stream_class(row.stream)
    cfg = cls.__eventic__
    rows = cfg.rows.read(s, cfg.stream, row.record_id, cfg.codec.window(), row.version)
    if not rows:
        raise RecordNotFound(cls.__name__, row.record_id, row.version)
    record = hydrate(cls, cfg.codec.decode(rows), rows[-1])
    event = Event(kind=row.kind, record=record, delta=row.delta)
    fn = _HANDLERS[row.handler_id]
    fn(event)
