"""Write/read orchestration — the canonical pipeline (CONCEPT §5–6).

``commit_version`` walks construct → before_commit (may veto) → encode →
persist → after_commit → emit → deliver, dispatching each stage to the record
class's assembled seam providers (defaults first, plugins after Step 6).
Reads dispatch through the codec's read-hint (``fetch``) then ``decode`` +
``after_hydrate``, so nothing above the codec seam knows how a version was
stored.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any, TYPE_CHECKING

from .events import Event
from .plugins import delivery_backends

if TYPE_CHECKING:
    from .record import Record

logger = logging.getLogger(__name__)

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


def _match(data: Any, filter_: dict[str, Any]) -> bool:
    """Python-side JSON containment (portable across SQLite/Postgres)."""
    if not isinstance(data, dict):
        return False
    return all(_get_path(data, k) == v for k, v in filter_.items())


def commit_version(
    cls: type["Record"],
    new: "Record",
    *,
    prev: "Record | None" = None,
    kind: str = "create",
    delta: dict | None = None,
) -> None:
    """Persist ``new`` as the next immutable version of its aggregate (I1/I4/I5).

    ``kind`` names the event emitted post-commit (I7) — ``"create"`` for
    ``save``, ``"update"`` for ``update``/``edit``/``commit``. ``prev`` is the
    previous version a diff codec needs; ``delta`` is the field-level change
    handed to update handlers.
    """
    # 1. before_commit interceptors (outer→inner; a failure/Veto aborts — the
    #    write has not happened yet, so failing loud is correct, PLUGINS §5)
    for itc in cls._interceptors:
        itc.before_commit(new)
    # 2. encode (exclusive codec seam)
    encoded = cls._codec.encode(prev, new)
    # 3. persist (exclusive persistence seam) — append-only, loud on conflicts.
    #    A byte-identical replay writes nothing, so no event fires (I7:
    #    exactly once per *commit* — a replay is not a commit).
    inserted = cls._persistence.append(
        {
            "version_id": new.version_id,
            "id": new.id,
            "version": new.version,
            "class_type": cls.__name__,
            "data": encoded,
        }
    )
    # 4. after_commit interceptors (inner→outer; failures isolated + logged)
    for itc in reversed(cls._interceptors):
        try:
            itc.after_commit(new)
        except Exception:
            logger.exception("after_commit interceptor %s failed", type(itc).__name__)
    # 5. emit -> deliver — strictly post-durable, exactly once (I7)
    if inserted:
        event = Event(kind=kind, record=new, delta=delta)
        for backend in delivery_backends():
            backend.deliver(event)


def _hydrate(cls: type["Record"], state: dict) -> "Record":
    obj = cls.model_validate(state)
    for itc in reversed(cls._interceptors):  # inner→outer (symmetric nesting)
        obj = itc.after_hydrate(obj)
    return obj


def read(cls: type["Record"], rec_id: uuid.UUID, *, version: int | None = None) -> "Record":
    """Hydrate one reconstructed, validated object (CONCEPT §6 read path)."""
    rows = cls._codec.fetch(cls._persistence, rec_id, cls.__name__, version=version)
    if not rows:
        suffix = f" v{version}" if version is not None else ""
        raise KeyError(f"{cls.__name__} {rec_id}{suffix} not found")
    state = cls._codec.decode(rows)
    return _hydrate(cls, state)


def history(cls: type["Record"], rec_id: uuid.UUID) -> list["Record"]:
    """Every version oldest→newest, each fully reconstructed (the log)."""
    rows = cls._persistence.stream(rec_id, cls.__name__)
    return [
        _hydrate(cls, cls._codec.decode(rows[: i + 1])) for i in range(len(rows))
    ]


def where(cls: type["Record"], **filters: Any) -> list["Record"]:
    """Latest records whose *reconstructed head* matches every (dotted-path)
    key/value pair — codec-aware (D14): a diff codec's latest row is a delta,
    so the match runs against ``codec.head_state``, never a raw row."""
    filter_ = {str(k): _jsonable(v) for k, v in filters.items()}
    ids: list[uuid.UUID] = []
    for rid, row in cls._persistence.latest_rows(cls.__name__):
        state = cls._codec.head_state(cls._persistence, cls.__name__, rid, row)
        if _match(state, filter_):
            ids.append(rid)
    return [read(cls, rid) for rid in ids]
