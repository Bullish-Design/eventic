"""
Thin data-access layer around `records` table.
All DBOS steps/transactions live here.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Iterator, List

from dbos import DBOS
from sqlalchemy import Select, insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .models import RecordRow


class RecordStore:
    """Thin data‑access layer around the `records` table."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def _new_session(self) -> Session:  # separate to keep pylint happy
        return Session(bind=self.engine, future=True)

    def latest_sync(self, rec_id: uuid.UUID) -> dict[str, Any]:
        with Session(self.engine, future=True) as s:
            q = (
                select(RecordRow.data)
                .where(RecordRow.id == rec_id)
                .order_by(RecordRow.version.desc())
                .limit(1)
            )
            row = s.execute(q).first()
            return row.data if row else {}

    def stream_sync(self, rec_id: uuid.UUID) -> Iterator[RecordRow]:
        with Session(self.engine, future=True) as s:
            q = (
                select(RecordRow)
                .where(RecordRow.id == rec_id)
                .order_by(RecordRow.version)
            )
            for r in s.execute(q):
                yield r.RecordRow  # type: ignore[attr-defined]

    # ---- writes ---------------------------------------------------------
    # @DBOS.transaction()
    # def append(self, rec: "Record", *, sql_session: Session) -> None:
    #    sql_session.execute(
    #        insert(RecordRow).values(
    #            version_id=rec.version_id,
    #            id=rec.id,
    #            version=rec.version,
    #            class_type=rec.__class__.__name__,
    #            properties=(
    #                rec.properties.model_dump(mode="json") if rec.properties else None
    #            ),
    #            data=rec.model_dump(mode="json"),
    #        )
    #    )

    def append(self, rec: "Record") -> None:  # type: ignore  # noqa: F821
        """
        Insert an immutable version row **exactly once**.

        • *Inside* a DBOS context → reuse the ambient DBOS.sql_session
        • *Outside* DBOS         → open a short-lived Session(engine)
        """
        row_vals = {
            "version_id": rec.version_id,
            "id": rec.id,
            "version": rec.version,
            "class_type": rec.__class__.__name__,
            "properties": (
                rec.properties.model_dump(mode="json") if rec.properties else None
            ),
            "data": rec.model_dump(mode="json"),
        }

        # session: Session | None = getattr(DBOS, "sql_session", None)

        # if session is not None:  # ← DBOS manages commit/rollback
        DBOS.sql_session.execute(insert(RecordRow).values(**row_vals))
        # else:  # ← standalone usage
        #    with Session(self.engine, future=True) as s:
        #        s.execute(insert(RecordRow).values(**row_vals))
        #        s.commit()

        # ---- reads ---------------------------------------------------------

    def stream(self, rec_id: uuid.UUID) -> Iterator[RecordRow]:
        """Yield rows *oldest→newest* synchronously (no DBOS context needed)."""
        with self._new_session() as s:
            q = (
                select(RecordRow)
                .where(RecordRow.id == rec_id)
                .order_by(RecordRow.version)
            )
            yield from (row for (row,) in s.execute(q))

    def latest(self, rec_id: uuid.UUID) -> Dict[str, Any]:
        """Return latest ``data`` snapshot for `rec_id` or empty dict."""
        with self._new_session() as s:
            q = (
                select(RecordRow.data)
                .where(RecordRow.id == rec_id)
                .order_by(RecordRow.version.desc())
                .limit(1)
            )
            row = s.execute(q).first()
            return row.data if row else {}

    def find_by_properties(self, filter_: Dict[str, Any]) -> List[uuid.UUID]:
        """Return ids whose **latest** properties JSONB contains `filter_`."""
        latest = (
            select(
                RecordRow.id.label("rid"),
                RecordRow.properties.label("props"),
            )
            .distinct(RecordRow.id)
            .order_by(RecordRow.id, RecordRow.version.desc())
        ).subquery()

        with self._new_session() as s:
            q = select(latest.c.rid).where(latest.c.props.contains(filter_))
            return [rid for (rid,) in s.execute(q)]

    '''
    # ---- reads ----------------------------------------------------------
    @DBOS.step()
    def stream(self, rec_id: uuid.UUID, *, sql_session: Session) -> Iterator[RecordRow]:
        q = select(RecordRow).where(RecordRow.id == rec_id).order_by(RecordRow.version)
        for row in sql_session.execute(q):
            yield row.RecordRow  # type: ignore[attr-defined]

    def stream(self, rec_id: uuid.UUID) -> Iterator[RecordRow]:
        """Yield rows *oldest→newest* synchronously (no DBOS context needed)."""
        with self._new_session() as s:
            q = (
                select(RecordRow)
                .where(RecordRow.id == rec_id)
                .order_by(RecordRow.version)
            )
            yield from (row for (row,) in s.execute(q))

    @DBOS.step()
    def latest(self, rec_id: uuid.UUID, *, sql_session: Session) -> Dict[str, Any]:
        q = (
            select(RecordRow.data)
            .where(RecordRow.id == rec_id)
            .order_by(RecordRow.version.desc())
            .limit(1)
        )
        row = sql_session.execute(q).first()
        return row.data if row else {}

    # ---- JSONB containment search --------------------------------------
    @DBOS.step()
    def find_by_properties(
        self, filter_: Dict[str, Any], *, sql_session: Session
    ) -> List[uuid.UUID]:
        """Return record ids whose *latest* properties JSONB contains `filter_`."""
        latest = (
            select(
                RecordRow.id.label("rid"),
                RecordRow.properties.label("props"),
            )
            .distinct(RecordRow.id)
            .order_by(RecordRow.id, RecordRow.version.desc())
        ).subquery()

        q = select(latest.c.rid).where(latest.c.props.contains(filter_))
        return [r.rid for r in sql_session.execute(q)]

    '''
