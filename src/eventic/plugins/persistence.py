"""Persistence seam — how & where version rows are stored and queried.

Default provider: ``SingleTableJSONB`` — the append-only ``records`` table.
``append`` upholds I1 (append-only), I4 (deterministic version_id) and I5
(loud conflicts): an ``IntegrityError`` on the ``(id, version)`` pair is a
*byte-identical replay* (same version_id **and** same encoded data) only when
it is a silent no-op; anything else is a different writer and raises
``StaleVersionError``.  [Deviation D2: the guide sketch compared only
``version_id``, which is content-independent under I4 and would therefore
classify *every* same-``(id,version)`` collision as a replay — silently
dropping the second writer. The check compares ``(version_id, data)``.]

``latest``/``at``/``stream``/``query`` are the row primitives the codec and the
read path consume, scoped by ``class_type`` (H4).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..connect import engine
from ..errors import StaleVersionError
from ..models import RecordRow
from . import Plugin, Seam

# Optional ambient-transaction hook: an adapter (eventic.dbos) registers a
# provider so appends can JOIN the surrounding transaction when one is active
# (the "persistence:transactional" capability). The default provider means
# "never ambient" — the core stays DBOS-free (I6).
_ambient_session: "Callable[[], Session | None]" = lambda: None


def set_ambient_session_provider(fn) -> None:
    """Register a callable returning the ambient session or None."""
    global _ambient_session
    _ambient_session = fn


def _reset_ambient_session_provider() -> None:
    global _ambient_session
    _ambient_session = lambda: None


class SingleTableJSONB(Plugin):
    """Append-only version log in the single ``records`` table (the default)."""

    seam = Seam.PERSISTENCE
    provides = {"persistence:json", "persistence:transactional"}
    requires = set()

    # ------------------------------------------------------------------ #
    # write
    # ------------------------------------------------------------------ #
    def append(self, row: dict) -> bool:
        """Insert one immutable version row; loud on real conflicts (I5).

        Returns ``True`` if a new row was written, ``False`` for a byte-
        identical replay no-op (I5). ``row``: version_id, id, version,
        class_type, data (created_ts filled by the column default).

        When an ambient transaction session is active (e.g. a DBOS
        transaction), the insert JOINS it — the outer transaction owns the
        commit, so a failed enclosing workflow rolls the row back (the
        ``persistence:transactional`` capability behind the transactional
        outbox). [Deviation D12: the first attempt used ``begin_nested()`` so
        a failed insert couldn't poison the ambient session, but SQLAlchemy's
        savepoint RELEASE + outer ROLLBACK does not actually roll back on
        SQLite — rows survived failed transactions. The check-then-insert
        shape avoids touching the session after any error; the only
        IntegrityError left is a lost race, which is loud by definition.]
        """
        ambient = _ambient_session()
        if ambient is not None:
            try:
                inserted = self._append_in(ambient, row)
            except IntegrityError:
                # lost a race inside the ambient transaction: the pre-check
                # saw no row, so whoever won is a different writer -> loud.
                # (The ambient session is now poisoned; the enclosing
                # transaction aborts, which is the correct outcome.)
                raise StaleVersionError(row["id"], row["version"]) from None
            return inserted
        with Session(engine(), future=True) as s:
            try:
                inserted = self._append_in(s, row)
                s.commit()
            except IntegrityError:
                s.rollback()
                return self._resolve_conflict(s, row)
            return inserted

    def _append_in(self, s: Session, row: dict) -> bool:
        """Check-then-insert: decide replay/conflict BEFORE touching the
        session, so a collision never poisons it (D12). Returns True if a row
        was inserted, False for a replay no-op."""
        existing = s.execute(
            select(RecordRow.version_id, RecordRow.data).where(
                RecordRow.id == row["id"],
                RecordRow.version == row["version"],
            )
        ).one_or_none()
        if existing is not None:
            self._decide(existing, row)
            return False
        s.execute(insert(RecordRow).values(**row))  # a race may raise IntegrityError
        return True

    def _decide(self, existing, row: dict) -> None:
        """I5: byte-identical replay is a silent no-op; anything else is loud."""
        if existing.version_id == row["version_id"] and existing.data == row["data"]:
            return
        raise StaleVersionError(row["id"], row["version"])

    def _resolve_conflict(self, s: Session, row: dict) -> bool:
        """Post-IntegrityError decision on our own session (already rolled
        back, so it is usable again)."""
        existing = s.execute(
            select(RecordRow.version_id, RecordRow.data).where(
                RecordRow.id == row["id"],
                RecordRow.version == row["version"],
            )
        ).one_or_none()
        if existing is not None:
            self._decide(existing, row)
            return False  # replay no-op
        raise StaleVersionError(row["id"], row["version"])

    # ------------------------------------------------------------------ #
    # reads (row primitives; the codec decides what it needs)
    # ------------------------------------------------------------------ #
    def latest(self, rec_id: uuid.UUID, class_type: str) -> RecordRow | None:
        with Session(engine(), future=True) as s:
            return s.execute(
                select(RecordRow)
                .where(RecordRow.id == rec_id, RecordRow.class_type == class_type)
                .order_by(RecordRow.version.desc(), RecordRow.version_id.desc())
                .limit(1)
            ).scalars().first()

    def at(self, rec_id: uuid.UUID, version: int, class_type: str) -> RecordRow | None:
        """Exact-version row (loud ``KeyError`` is the caller's job)."""
        with Session(engine(), future=True) as s:
            return s.execute(
                select(RecordRow).where(
                    RecordRow.id == rec_id,
                    RecordRow.version == version,
                    RecordRow.class_type == class_type,
                )
            ).scalars().first()

    def stream(self, rec_id: uuid.UUID, class_type: str) -> list[RecordRow]:
        """All rows for the aggregate, oldest→newest (the log, I1)."""
        with Session(engine(), future=True) as s:
            return list(
                s.execute(
                    select(RecordRow)
                    .where(RecordRow.id == rec_id, RecordRow.class_type == class_type)
                    .order_by(RecordRow.version)
                ).scalars()
            )

    def latest_rows(self, class_type: str) -> list[tuple[uuid.UUID, RecordRow]]:
        """(id, latest row) for every aggregate of this class (D14: the
        codec's ``head_state`` reconstructs the true head from these)."""
        rn = func.row_number().over(
            partition_by=RecordRow.id, order_by=RecordRow.version.desc()
        ).label("rn")
        sub = (
            select(
                RecordRow.id.label("rid"),
                RecordRow.version_id.label("version_id"),
                RecordRow.version.label("version"),
                RecordRow.class_type.label("class_type"),
                RecordRow.created_ts.label("created_ts"),
                RecordRow.data.label("data"),
                rn,
            )
            .where(RecordRow.class_type == class_type)
            .subquery()
        )
        with Session(engine(), future=True) as s:
            rows = s.execute(select(sub).where(sub.c.rn == 1)).all()
        return [
            (
                r.rid,
                RecordRow(
                    version_id=r.version_id,
                    id=r.rid,
                    version=r.version,
                    class_type=r.class_type,
                    created_ts=r.created_ts,
                    data=r.data,
                ),
            )
            for r in rows
        ]

    def query(self, class_type: str, filter_: dict[str, Any]) -> list[uuid.UUID]:
        """DEPRECATED read primitive kept for the guide's module map; the
        pipeline's ``where()`` uses ``latest_rows`` + the codec's
        ``head_state`` instead (D14) so diff-stored classes match on the true
        head, not on a delta row."""
        rows = self.latest_rows(class_type)
        from ..pipeline import _match

        filter_ = {str(k): _jsonable(v) for k, v in filter_.items()}
        return [rid for rid, row in rows if _match(row.data, filter_)]


class TypedTable(Plugin):
    """Persistence provider that gives each Record its own typed-column table.

    **Demonstration of reach only — NOT implemented** (PLUGINS §8.5; see
    ``TARGET_ARCHITECTURE.md`` §1 for the SQLModel sketch). It exists so the
    ``DiffStorage + TypedTable`` incompatibility can be proven at class
    definition (Step 8's guardrail test).
    """

    seam = Seam.PERSISTENCE
    provides = {"persistence", "persistence:columns", "persistence:transactional"}
    requires = set()
