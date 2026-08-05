"""``Record`` — a plain Pydantic v2 model that becomes a versioned aggregate.

Pure construction (I3): ``Todo(...)`` validates and assigns identity *in
memory only* — no I/O, no events, no auto-persist. Writes are explicit
(``save``/``update``/``edit``/``commit``, added in Step 3). ``version_id`` is
the deterministic ``uuid5`` of ``(id, version)`` for **every** version,
including v0 (I4, closes R-C2).

No metaclass, no singleton, no copy-on-write ``__setattr__``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


def _uuid5(id: uuid.UUID, version: int) -> uuid.UUID:
    """Deterministic version identity (I4): the same (id, version) always
    yields the same version_id — for replays, retries, and v0."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"eventic:{id}:{version}")


class Record(BaseModel):
    """Base class — construction is pure; persistence is explicit."""

    model_config = {"extra": "allow", "arbitrary_types_allowed": True}  # NOT frozen

    id: uuid.UUID = Field(default_factory=uuid.uuid4)  # stable aggregate identity
    version: int = 0
    version_id: uuid.UUID | None = None  # deterministic (I4); None until stamped
    created_ts: datetime | None = None
    meta: dict[str, Any] = Field(default_factory=dict)  # free-form metadata bag

    def model_post_init(self, _):
        """PURE: stamp v0's deterministic identity only — never any I/O (I3)."""
        if self.version_id is None:
            object.__setattr__(self, "version_id", _uuid5(self.id, self.version))
