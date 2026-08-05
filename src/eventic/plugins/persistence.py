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

import datetime as dt
import uuid
from typing import Any, Iterable

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..connect import engine
from ..errors import StaleVersionError
from ..models import RecordRow

_MISSING = object()


def _jsonable(value: Any) -> Any:
    """Normalize filter values to what the JSON column actually stores."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def _get_path(data: Any, path: str) -> Any:
    """Dotted-path lookup: ``"meta.status"`` → ``data["meta"]["status"]``."""
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def _dict_contains(data: Any, filter_: dict[str, Any]) -> bool:
    """Python-side JSON containment (portable across SQLite/Postgres)."""
    if not isinstance(data, dict):
        return False
    return all(_get_path(data, k) == v for k, v in filter_.items())


class SingleTableJSONB:
    """Append-only version log in the single ``records`` table."""

    provides = {"persistence:json", "persistence:transactional"}

    # ------------------------------------------------------------------ #
    # write
    # ------------------------------------------------------------------ #
    def append(self, row: dict) -> None:
        """Insert one immutable version row; loud on real conflicts (I5).

        ``row``: version_id, id, version, class_type, data (created_ts is
        filled by the column default).
        """
        with Session(engine(), future=True) as s:
            try:
                s.execute(insert(RecordRow).values(**row))
                s.commit()
            except IntegrityError:  # (id, version) collision
                s.rollback()
                existing = s.execute(
                    select(RecordRow.version_id, RecordRow.data).where(
                        RecordRow.id == row["id"],
                        RecordRow.version == row["version"],
                    )
                ).one_or_none()
                if existing is not None and (
                    existing.version_id == row["version_id"]
                    and existing.data == row["data"]
                ):
                    return  # byte-identical replay -> idempotent no-op (I5)
                raise StaleVersionError(row["id"], row["version"])  # different writer -> LOUD

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

    def query(self, class_type: str, filter_: dict[str, Any]) -> list[uuid.UUID]:
        """Ids whose **latest** version's data matches every (dotted) key."""
        filter_ = {str(k): _jsonable(v) for k, v in filter_.items()}
        rn = func.row_number().over(
            partition_by=RecordRow.id, order_by=RecordRow.version.desc()
        ).label("rn")
        latest = (
            select(RecordRow.id.label("rid"), RecordRow.data.label("data"), rn)
            .where(RecordRow.class_type == class_type)
            .subquery()
        )
        with Session(engine(), future=True) as s:
            rows = s.execute(
                select(latest.c.rid, latest.c.data).where(latest.c.rn == 1)
            ).all()
        return [rid for rid, data in rows if _dict_contains(data, filter_)]
