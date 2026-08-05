"""Write/read orchestration — the canonical pipeline (CONCEPT §5–6).

``commit_version`` walks construct → before_commit (may veto) → encode →
persist → after_commit → emit → deliver, dispatching each stage to the record
class's assembled seam providers (defaults first, plugins after Step 6).
Reads dispatch through the codec's read-hint (``fetch``) then ``decode`` +
``after_hydrate``, so nothing above the codec seam knows how a version was
stored.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from .eventbus import Event
from .plugins import delivery_backends

if TYPE_CHECKING:
    from .record import Record

logger = logging.getLogger(__name__)


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
    # 3. persist (exclusive persistence seam) — append-only, loud on conflicts
    cls._persistence.append(
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
