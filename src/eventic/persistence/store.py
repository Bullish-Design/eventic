"""
Thin data-access layer around the `records` table.

One session strategy (C1 + H2): every read and write goes through
``RecordStore._session()``, which prefers the ambient DBOS transaction
session and falls back to a short-lived session on the store's own engine.
This makes mutations work in plain scripts *and* makes reads inside a DBOS
transaction see the transaction's own uncommitted writes.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List

from dbos import DBOS
from sqlalchemy import insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .models import RecordRow


class RecordStore:
    """Thin data-access layer around the `records` table."""

    def __init__(self, engine: Engine):
        self.engine = engine

    @contextmanager
    def _session(self):
        """Ambient DBOS transaction session if inside one; otherwise our own
        short-lived session (committed on clean exit). Fixes C1 + H2."""
        try:
            ambient = DBOS.sql_session
        except AssertionError:
            ambient = None
        if ambient is not None:
            yield ambient  # DBOS owns commit/rollback
            return
        with Session(self.engine, future=True) as s:
            yield s
            s.commit()  # standalone path — skipped on exception

    def append(self, rec: "Record") -> None:
        """Insert an immutable version row **exactly once**."""
        row_vals = {
            "version_id": rec.version_id,
            "id": rec.id,
            "version": rec.version,
            "class_type": rec.__class__.__name__,
            "properties": (
                rec.properties.model_dump(mode="json") if rec.properties else {}
            ),
            "data": rec.model_dump(mode="json"),
        }
        with self._session() as s:
            s.execute(insert(RecordRow).values(**row_vals))

    def latest(self, rec_id: uuid.UUID, class_type: str | None = None) -> Dict[str, Any]:
        """Latest committed data snapshot for rec_id (optionally of one class).

        Tie-breaks by ``version_id.desc()`` so identical version numbers
        resolve deterministically (C6).
        """
        with self._session() as s:
            q = (
                select(RecordRow.data)
                .where(RecordRow.id == rec_id)
                .order_by(RecordRow.version.desc(), RecordRow.version_id.desc())
                .limit(1)
            )
            if class_type:
                q = q.where(RecordRow.class_type == class_type)
            row = s.execute(q).first()
            return row.data if row else {}

    def stream(self, rec_id: uuid.UUID, class_type: str | None = None):
        """Yield rows *oldest→newest*, optionally filtered by class_type (H4)."""
        with self._session() as s:
            q = (
                select(RecordRow)
                .where(RecordRow.id == rec_id)
                .order_by(RecordRow.version)
            )
            if class_type:
                q = q.where(RecordRow.class_type == class_type)
            yield from (row for (row,) in s.execute(q))

    def find_by_properties(self, filter_: Dict[str, Any]) -> List[uuid.UUID]:
        """Return ids whose **latest** properties JSONB contains `filter_`.

        Rewritten portably in Step 4.2 (C4/H4/M4) — the current query relies
        on Postgres-only DISTINCT ON and dialect-specific JSON containment.
        """
        latest = (
            select(
                RecordRow.id.label("rid"),
                RecordRow.properties.label("props"),
            )
            .distinct(RecordRow.id)
            .order_by(RecordRow.id, RecordRow.version.desc())
        ).subquery()

        with Session(self.engine, future=True) as s:
            q = select(latest.c.rid).where(latest.c.props.contains(filter_))
            return [rid for (rid,) in s.execute(q)]
