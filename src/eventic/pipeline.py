"""Write/read orchestration — the canonical pipeline (CONCEPT §5–6).

``commit_version`` walks construct → encode → persist (the exclusive seam
providers dispatch through the record class's attached defaults; interceptors
and delivery join in Steps 5/6). Reads dispatch through the codec's read-hint
(``fetch``) then ``decode``, so nothing above the codec seam knows how a
version was stored.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .record import Record


def commit_version(cls: type["Record"], new: "Record", *, prev: "Record | None" = None, kind: str = "create") -> None:
    """Persist ``new`` as the next immutable version of its aggregate (I1/I4/I5).

    ``kind`` is one of ``"create"``/``"update"`` and names the event emitted
    post-commit (Step 5). ``prev`` is the previous version the codec may need
    (a diff codec derives the delta from it).
    """
    # 1. before_commit interceptors (Step 6; none by default)
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
    # 4. after_commit interceptors (Step 6)
    # 5. emit -> deliver (Step 5; strictly post-durable, I7)


def read(cls: type["Record"], rec_id: uuid.UUID, *, version: int | None = None) -> "Record":
    """Hydrate one reconstructed, validated object (CONCEPT §6 read path)."""
    rows = cls._codec.fetch(cls._persistence, rec_id, cls.__name__, version=version)
    if not rows:
        suffix = f" v{version}" if version is not None else ""
        raise KeyError(f"{cls.__name__} {rec_id}{suffix} not found")
    state = cls._codec.decode(rows)
    return cls.model_validate(state)


def history(cls: type["Record"], rec_id: uuid.UUID) -> list["Record"]:
    """Every version oldest→newest, each fully reconstructed (the log)."""
    rows = cls._persistence.stream(rec_id, cls.__name__)
    return [cls.model_validate(cls._codec.decode(rows[: i + 1])) for i in range(len(rows))]
