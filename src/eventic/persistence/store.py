"""
Thin data-access layer around the `records` table.

One session strategy (C1 + H2): every read and write goes through
``RecordStore._session()``, which prefers the ambient DBOS transaction
session and falls back to a short-lived session on the store's own engine.
This makes mutations work in plain scripts *and* makes reads inside a DBOS
transaction see the transaction's own uncommitted writes.
"""

from __future__ import annotations

import datetime as dt
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List

from dbos import DBOS
from sqlalchemy import func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .models import RecordRow


def _jsonable(value: Any) -> Any:
    """Normalize filter values to what the JSON column actually stores (M4)."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def _dict_contains(props: Any, filter_: Dict[str, Any]) -> bool:
    """Python-side JSON containment for non-Postgres backends (SQLite)."""
    if not isinstance(props, dict):
        return False
    return all(props.get(k) == v for k, v in filter_.items())


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
        """Insert an immutable version row **exactly once**.

        ``ON CONFLICT DO NOTHING`` makes crash-recovery replays of the same
        ``(id, version)`` row a no-op (deterministic version_id + the
        ``uq_records_id_version`` constraint) instead of a duplicate (C6).
        """
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
        # on_conflict_do_nothing is dialect-specific (Postgres/SQLite share the
        # same generic syntax here); pick the right insert() for this engine.
        if self.engine.dialect.name == "postgresql":
            stmt = pg_insert(RecordRow).values(**row_vals).on_conflict_do_nothing()
        else:
            stmt = sqlite_insert(RecordRow).values(**row_vals).on_conflict_do_nothing()
        with self._session() as s:
            s.execute(stmt)

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

    def find_by_properties(
        self, class_type: str, filter_: Dict[str, Any]
    ) -> List[uuid.UUID]:
        """Return ids whose **latest** properties JSONB contains `filter_`.

        H4: rows are pre-filtered by ``class_type`` (matches ``cls.__name__``
        exactly). Containment is dialect-aware: Postgres uses the native
        ``jsonb @>`` operator; other backends (SQLite) filter in Python after
        a portable window-function latest-per-id pass.
        """
        filter_ = {k: _jsonable(v) for k, v in filter_.items()}
        rn = func.row_number().over(
            partition_by=RecordRow.id, order_by=RecordRow.version.desc()
        ).label("rn")
        latest = (
            select(
                RecordRow.id.label("rid"),
                RecordRow.properties.label("props"),
                rn,
            )
            .where(RecordRow.class_type == class_type)
            .subquery()
        )

        with self._session() as s:
            if s.get_bind().dialect.name == "postgresql":
                q = select(latest.c.rid).where(
                    latest.c.rn == 1, latest.c.props.contains(filter_)
                )
                return [rid for (rid,) in s.execute(q)]
            rows = s.execute(
                select(latest.c.rid, latest.c.props).where(latest.c.rn == 1)
            ).all()
            return [rid for rid, props in rows if _dict_contains(props, filter_)]
