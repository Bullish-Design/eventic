"""Write/read orchestration — the pipeline (CONCEPT §4/§5).

``commit`` walks construct → before_commit (may veto, return threaded — F11) →
encode → append, then projects the head, stages outbox rows, and **stages** the
``Event`` on the unit of work. The transaction flushes after ``COMMIT`` — the
pipeline never emits (I7/F3). Reads go through the head (``get``/``where``) or
a bounded log window (historical reads); hydration merges commit metadata back
from columns (F5 without breaking I5, CONCEPT §5.1).
"""

from __future__ import annotations

import uuid
from typing import Any

from .errors import RecordNotFound
from .event import Event
from .identity import version_id
from .state import user_state
from .store import active_store
from .store.schema import HeadRow, LogRow
from .subscribe import subscriptions_for

_MISSING = object()


def commit(cls: type, new, *, prev=None, kind: str = "create", changes: dict | None = None) -> None:
    """Persist ``new`` as the next immutable version of its aggregate (I1/I4/I5).

    ``kind`` names the event emitted post-commit (I7): ``"create"`` for
    ``save``, ``"update"`` for ``update``/``draft().commit()``. A byte-
    identical replay inserts nothing, so nothing is staged and nothing emits.
    """
    cfg = cls.__eventic__
    # 1. before_commit interceptors (outer→inner; a failure/Veto aborts the
    #    write before it happens). The return value IS threaded (F11).
    for itc in cfg.interceptors:
        new = itc.before_commit(new)
    # 2. encode (exclusive codec seam)
    data, is_snapshot = cfg.codec.encode(prev, new)
    # 3. one transaction: log + head + outbox, then a staged event
    uow = active_store().unit_of_work()
    with uow:
        row = LogRow(
            version_id=new.version_id,
            stream=cfg.stream,
            id=new.id,
            version=new.version,
            kind=kind,
            snapshot=is_snapshot,
            data=data,
        )
        inserted = cfg.rows.append(uow.session, row)
        if inserted:
            event = Event(kind=kind, record=new, delta=changes)
            cfg.rows.upsert_head(
                uow.session,
                HeadRow(
                    stream=cfg.stream,
                    id=new.id,
                    version=new.version,
                    version_id=new.version_id,
                    committed_at=row.committed_at,
                    state=user_state(new),
                ),
            )
            for sub in subscriptions_for(cls):
                if sub.via == "outbox" and sub.matches(kind):
                    cfg.rows.stage_outbox(uow.session, sub, event)
            uow.stage(event)
    # emission happens here, after COMMIT, via the unit of work (I7)


# ---------------------------------------------------------------------- #
# reads
# ---------------------------------------------------------------------- #
def hydrate(cls: type, state: dict, row) -> Any:
    """Merge commit metadata from the row and run after_hydrate (inner→outer).

    ``created_ts`` is stamped from ``committed_at`` here — never from ``data``
    — which is what keeps I5's byte-identical replays stable (F5, §5.1).
    """
    obj = cls.model_validate(
        state
        | {
            "id": row.id,
            "version": row.version,
            "version_id": row.version_id,
            "created_ts": row.committed_at,
        }
    )
    for itc in reversed(cls.__eventic__.interceptors):
        obj = itc.after_hydrate(obj)
    return obj


def read(cls: type, rec_id: uuid.UUID, *, version: int | None = None) -> Any:
    """Hydrate one reconstructed, validated object (CONCEPT §5 read path)."""
    cfg = cls.__eventic__
    with active_store().session() as s:
        if version is None:
            head = cfg.rows.head(s, cfg.stream, rec_id)
            if head is None:
                raise RecordNotFound(cls.__name__, rec_id)
            return hydrate(cls, head.state, head)
        rows = cfg.rows.read(s, cfg.stream, rec_id, cfg.codec.window(), version)
        if not rows:
            raise RecordNotFound(cls.__name__, rec_id, version)
        return hydrate(cls, cfg.codec.decode(rows), rows[-1])


def history(cls: type, rec_id: uuid.UUID) -> list[Any]:
    """Every version oldest→newest — a single forward fold (F19)."""
    cfg = cls.__eventic__
    with active_store().session() as s:
        rows = cfg.rows.stream(s, cfg.stream, rec_id)
        if not rows:
            raise RecordNotFound(cls.__name__, rec_id)
        return [hydrate(cls, state, row) for state, row in cfg.codec.iter_states(rows)]


def _jsonable(value: Any) -> Any:
    """Normalize filter values to what the JSON column actually stores."""
    import datetime as dt

    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def where(cls: type, **filters: Any) -> list[Any]:
    """Latest records whose head state matches every (dotted-path) key/value.

    Equality pushdown happens in the store (F16); hydration is in-memory from
    the head rows, so one query answers the whole call.
    """
    cfg = cls.__eventic__
    filter_ = {str(k): _jsonable(v) for k, v in filters.items()}
    with active_store().session() as s:
        heads = cfg.rows.search(s, cfg.stream, filter_)
        return [hydrate(cls, h.state, h) for h in heads]


# ---------------------------------------------------------------------- #
# derived projections
# ---------------------------------------------------------------------- #
def rebuild_heads(store, stream: str | None = None) -> int:
    """Rebuild ``eventic_head`` from the log — the honesty check on §2.1: if
    the projection cannot be rebuilt from the log, the log is not the truth.

    The fold is codec-agnostic by construction: a snapshot row's ``data`` is
    the full user state and a delta row is ``{"set": ..., "del": [...]}`` —
    so rebuilding needs only the log, never the app's classes (which is what
    lets the CLI do it).
    """
    from sqlalchemy import select

    from .store.schema import HeadRow, LogRow
    from .store.sql import SqlStore

    rows_store = SqlStore()
    rebuilt = 0
    with store.session() as s:
        streams_stmt = select(LogRow.stream).distinct()
        if stream is not None:
            streams_stmt = streams_stmt.where(LogRow.stream == stream)
        for name in s.execute(streams_stmt).scalars():
            groups: dict = {}
            for row in rows_store.all_rows(s, name):
                groups.setdefault(row.id, []).append(row)
            for rid, group in groups.items():
                state: dict = {}
                for row in group:
                    if row.snapshot:
                        state = dict(row.data)
                    else:
                        state = {**state, **row.data.get("set", {})}
                        for key in row.data.get("del", []):
                            state.pop(key, None)
                last = group[-1]
                rows_store.upsert_head(
                    s,
                    HeadRow(
                        stream=name,
                        id=rid,
                        version=last.version,
                        version_id=last.version_id,
                        committed_at=last.committed_at,
                        state=state,
                    ),
                    force=True,  # a rebuild must be able to repair a head
                )
                rebuilt += 1
        s.commit()
    return rebuilt

