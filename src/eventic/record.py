"""``Record`` — a plain Pydantic v2 model that becomes a versioned aggregate.

Pure construction (I3): ``Todo(...)`` validates and assigns identity *in
memory only* — no I/O, no events, no auto-persist. Writes are explicit:
``save`` (v0), ``update`` (new version, original untouched), ``edit``
(batched, Step 4), ``commit`` (low-level next version). ``version_id`` is the
deterministic ``uuid5`` of ``(id, version)`` for **every** version including
v0 (I4, closes R-C2). Reads: ``get``/``history``/``where``.

No metaclass, no singleton, no copy-on-write ``__setattr__``. The three
exclusive seam defaults are wired as class attributes (Step 6 replaces them
with the assembled plugin set).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, ClassVar, Self

from pydantic import BaseModel, Field

from . import pipeline
from .plugins.codec import FullSnapshot
from .plugins.identity import Uuid5Deterministic, _uuid5
from .plugins.persistence import SingleTableJSONB


class Record(BaseModel):
    """Base class — construction is pure; persistence is explicit."""

    model_config = {"extra": "allow", "arbitrary_types_allowed": True}  # NOT frozen

    id: uuid.UUID = Field(default_factory=uuid.uuid4)  # stable aggregate identity
    version: int = 0
    version_id: uuid.UUID | None = None  # deterministic (I4); None until stamped
    created_ts: datetime | None = None
    meta: dict[str, Any] = Field(default_factory=dict)  # free-form metadata bag

    # exclusive-seam defaults (the null plugins, CONCEPT §7) — swapped by the
    # class assembler at Step 6
    _persistence: ClassVar[SingleTableJSONB] = SingleTableJSONB()
    _codec: ClassVar[FullSnapshot] = FullSnapshot()
    _identity: ClassVar[Uuid5Deterministic] = Uuid5Deterministic()

    def model_post_init(self, _):
        """PURE: stamp v0's deterministic identity only — never any I/O (I3)."""
        if self.version_id is None:
            object.__setattr__(self, "version_id", _uuid5(self.id, self.version))

    # ------------------------------------------------------------------ #
    # explicit writes (I2 — nothing else persists)
    # ------------------------------------------------------------------ #
    def save(self) -> Self:
        """Persist v0 explicitly. Byte-identical replays of ``(id, 0)`` are an
        idempotent no-op (I5); the aggregate keeps exactly one v0 row."""
        pipeline.commit_version(type(self), new=self, prev=None, kind="create")
        return self

    def update(self, **changes: Any) -> Self:
        """Validate a **new** version with ``changes`` applied and persist it.
        Returns the new object; the original is untouched (R-C3)."""
        data = self.model_dump(mode="python")
        data.update(changes)
        data["version"] = self.version + 1
        data["version_id"] = _uuid5(self.id, data["version"])
        data.pop("created_ts", None)  # the row's DB default stamps it
        new = type(self)(**data)
        pipeline.commit_version(type(self), new=new, prev=self, kind="update")
        return new

    def commit(self) -> Self:
        """Low-level: persist the current in-memory state as the next version."""
        return self.update()

    # ------------------------------------------------------------------ #
    # reads (CONCEPT §6 read path)
    # ------------------------------------------------------------------ #
    @classmethod
    def get(cls, rec_id: uuid.UUID, version: int | None = None) -> Self:
        """Exact ``version`` (default latest). Loud ``KeyError`` if absent."""
        return pipeline.read(cls, rec_id, version=version)

    @classmethod
    def history(cls, rec_id: uuid.UUID) -> list[Self]:
        """Every version oldest→newest, fully reconstructed."""
        return pipeline.history(cls, rec_id)

    @classmethod
    def where(cls, **filters: Any) -> list[Self]:
        """Latest records whose data matches every (dotted-path) key/value pair.
        Scoped to this class (``class_type``); a JSONB convenience (R-P2)."""
        ids = cls._persistence.query(cls.__name__, filters)
        return [cls.get(rid) for rid in ids]
