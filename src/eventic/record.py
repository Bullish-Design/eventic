"""``Record`` and ``Draft`` — frozen values, explicit writes (CONCEPT §6).

A ``Record`` is one immutable version — construction is pure (I3), nothing is
written until ``save``/``update``/``draft().commit()`` (I2). ``frozen=True``
and ``extra="forbid"`` make I1 and I3 pydantic-enforced (F14) and delete every
``object.__setattr__`` workaround of v2 — including the ``hair_trigger`` flag
whose only purpose was disabling I2.

``Draft`` is the only mutation affordance: ``record.draft()`` yields a mutable
scratch copy; ``draft.commit()`` writes one new version and **returns it**
(F6) — every write returns the new value; nothing mutates in place.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import pipeline
from .config import DEFAULT_CONFIG, resolve_config
from .errors import UsageError
from .identity import version_id

MANAGED = frozenset({"id", "version", "version_id", "created_ts"})


class Record(BaseModel):
    """Base class — construction is pure; persistence is explicit."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)  # stable aggregate identity
    version: int = 0
    version_id: uuid.UUID | None = None  # deterministic (I4); derived, never client-set
    created_ts: datetime | None = None  # commit metadata; stamped at hydration (F5)
    meta: dict[str, Any] = Field(default_factory=dict)  # free-form metadata bag

    # class-level declarations (dunder names: never model fields)
    __eventic__ = DEFAULT_CONFIG
    __subscriptions__: ClassVar[list] = []

    def __init_subclass__(
        cls,
        *,
        stream: str | None = None,
        rows=None,
        codec=None,
        interceptors=None,
        **kw,
    ):
        super().__init_subclass__()  # pydantic's takes no kwargs
        cls.__eventic__ = resolve_config(
            cls, stream=stream, rows=rows, codec=codec, interceptors=interceptors
        )
        cls.__subscriptions__ = []  # per-class; inherited via MRO walk

    @model_validator(mode="before")
    @classmethod
    def _derive_identity(cls, data):
        """I4: version_id is always computed, never client-set. ``id``/``version``
        default here so construction stays pure and hydration cannot override."""
        if isinstance(data, dict):
            data = dict(data)
            data.setdefault("id", uuid.uuid4())
            data.setdefault("version", 0)
            data["version_id"] = version_id(data["id"], data["version"])
        return data

    # ------------------------------------------------------------------ #
    # explicit writes (I2 — nothing else persists)
    # ------------------------------------------------------------------ #
    def save(self) -> Self:
        """Persist v0 explicitly. Byte-identical replays of ``(id, 0)`` are an
        idempotent no-op (I5); the aggregate keeps exactly one v0 row."""
        if self.version != 0:
            raise UsageError("save() persists v0; use update() for later versions")
        pipeline.commit(type(self), new=self, prev=None, kind="create")
        return self

    def update(self, **changes: Any) -> Self:
        """Validate a **new** version with ``changes`` applied and persist it.
        Returns the new object; the original is untouched. Managed fields are
        always derived, never client-set."""
        data = self.model_dump(mode="python", exclude=MANAGED)
        data.update(changes)
        data["id"] = self.id
        data["version"] = self.version + 1
        data["version_id"] = version_id(self.id, data["version"])
        data.pop("created_ts", None)
        new = type(self)(**data)
        pipeline.commit(
            type(self), new=new, prev=self, kind="update", changes=dict(changes)
        )
        return new

    def draft(self) -> "Draft":
        """A mutable scratch copy. Nothing is written until ``commit()``."""
        return Draft(self)

    # ------------------------------------------------------------------ #
    # reads (CONCEPT §5 read path)
    # ------------------------------------------------------------------ #
    @classmethod
    def get(cls, rec_id: uuid.UUID, version: int | None = None) -> Self:
        """Exact ``version`` (default latest). Loud ``RecordNotFound`` if absent."""
        return pipeline.read(cls, rec_id, version=version)

    @classmethod
    def history(cls, rec_id: uuid.UUID) -> list[Self]:
        """Every version oldest→newest, fully reconstructed."""
        return pipeline.history(cls, rec_id)

    @classmethod
    def where(cls, **filters: Any) -> list[Self]:
        """Latest records whose head matches every (dotted-path) key/value."""
        return pipeline.where(cls, **filters)


class Draft:
    """Mutable scratch copy of a ``Record``. ``commit()`` returns the NEW
    version — assignment is the point (F6). No context-manager form exists,
    deliberately: a ``with`` block cannot return a value, which is precisely
    the mechanism that stranded v2 callers on a stale handle."""

    def __init__(self, base: Record):
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_data", base.model_dump(mode="python"))

    def __getattr__(self, name: str):
        try:
            return self._data[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            self._data[name] = value

    def _changes(self) -> dict[str, Any]:
        before = self._base.model_dump(mode="python")
        return {
            k: v
            for k, v in self._data.items()
            if k not in MANAGED and before.get(k) != v
        }

    def commit(self) -> Record:
        """Write one new version from the scratch state and return it."""
        changes = self._changes()
        return self._base if not changes else self._base.update(**changes)
