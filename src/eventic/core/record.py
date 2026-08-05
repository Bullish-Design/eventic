"""
Record / Hydrate kernel – *pure Pydantic* (no Postgres or DBOS imports).

* Any attribute write ➜ copy-on-write ➜ new immutable version ➜ store.append()
* Methods explicitly marked with @evented are scheduled on the per-class DBOS
  queue (opt-in; everything else is left untouched — see queues.dispatcher).
"""

from __future__ import annotations

import re
import uuid
from typing import Any, ClassVar, Type, TypeVar

from pydantic import BaseModel, Field

from dbos import Queue
from .properties import PropertiesBase


T_Record = TypeVar("T_Record", bound="Record")
ModelMeta = BaseModel.__class__


# helpers
def _snake(name: str) -> str:
    """CamelCase ➜ snake_case."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


# metaclass that attaches per-class metadata + wraps ONLY @evented methods
class RecordMeta(ModelMeta):
    """Attach `_queue_name` and a single per-class Queue; wrap only methods
    explicitly marked with @evented (C2/C3/M7)."""

    def __new__(mcls, name: str, bases, ns, **kw):
        cls = super().__new__(mcls, name, bases, ns)  # create class first
        if name == "Record":  # skip abstract base
            return cls

        cls._queue_name = f"queue_{_snake(name)}"
        cls.queue = Queue(cls._queue_name, concurrency=1)  # single declaration (M7)

        # late import – avoids circular dep
        from eventic.queues.dispatcher import _queue_method

        for attr, fn in ns.items():
            if getattr(fn, "__eventic_evented__", False):  # ONLY explicitly marked
                setattr(cls, attr, _queue_method(fn))
            # everything else — staticmethods, classmethods, properties, and any
            # model_* internals — is left completely untouched (C3)

        return cls


# Record base
class Record(BaseModel, metaclass=RecordMeta):
    """Base class – attribute mutation creates a *new* immutable version row."""

    id: uuid.UUID | None = None  # Field(default_factory=uuid.uuid4)
    version_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    version: int = 0
    properties: PropertiesBase | None = None

    _store: ClassVar["RecordStore" | None] = None  # injected by init_eventic()
    model_config = {"frozen": True, "extra": "allow", "arbitrary_types_allowed": True}

    # ensure properties exists & has record_type; persist durable v0 (C5)
    def model_post_init(self, _ctx):
        is_new = self.id is None

        if self.id is None:
            object.__setattr__(self, "id", uuid.uuid4())
        if self.properties is None:
            object.__setattr__(
                self,
                "properties",
                PropertiesBase(record_type=self.__class__.__name__),
            )
            self.properties._bind(self)  # H1 — see Step 5.1
        elif self.properties.record_type == "":
            object.__setattr__(self.properties, "record_type", self.__class__.__name__)
            self.properties._bind(self)

        if is_new and self.version == 0:
            from eventic.events import emit_create

            if self._store is not None:
                self._store.append(self)  # durable v0 — the row now exists
            emit_create(self)  # handlers can hydrate (H6 timing)

    # copy‑on‑write mutation
    def __setattr__(self, name: str, value: Any):
        if name.startswith("_"):
            return super().__setattr__(name, value)

        data = self.model_dump(mode="python")
        data[name] = value

        data["version"] = self.version + 1
        data["version_id"] = str(uuid.uuid4())

        new_obj = self.__class__(**data)  # type: ignore[arg-type]
        self._ensure_store()
        self._store.append(new_obj)  # type: ignore[union-attr]

        # reflect locally
        object.__setattr__(self, name, value)
        object.__setattr__(self, "version", data["version"])
        object.__setattr__(self, "version_id", uuid.UUID(data["version_id"]))

        # Emit update event
        from eventic.events import emit_update

        emit_update(self)

    @classmethod
    def where(cls: type[T_Record], **filters: Any) -> list[T_Record]:
        """
        Return records whose JSONB properties match *all* of the given key/value
        pairs.  Example: Story.where(status="published", audience="kids")
        """
        return cls._store.find_by_properties(filters)

    # hydration
    @classmethod
    def hydrate(
        cls: Type[T_Record], rec_id: uuid.UUID, version: int | None = None
    ) -> T_Record:
        cls._ensure_store()
        if version is None:
            state = cls._store.latest(rec_id)  # Or latest_sync?
            if not state:
                raise KeyError(
                    f"{cls.__name__} {rec_id} not found (no committed rows yet)"
                )
            return cls.model_validate(state)
        obj: T_Record | None = None
        for row in cls._store.stream(rec_id):  # type: ignore[union-attr]
            if row.version > version:
                break
            obj = cls.model_validate(row.data)
        if obj is None:
            raise KeyError(f"{cls.__name__} {rec_id} ≤ v{version} not found")
        return obj

    # internal util
    @classmethod
    def _ensure_store(cls):
        if cls._store is None:
            raise RuntimeError("Call init_eventic(engine) before using Record")
