"""
Free-form metadata bag that lives on every Record instance.

* Auto-populates `record_type` with the class name.
* `add`/`remove` mutate the bag **and** write a new record version (H1) — the
  mutation happens in place first, then ``_persist`` commits a new version
  directly through the owner (bypassing ``Record.__setattr__``'s no-op guard,
  which would otherwise see the in-place change and skip the write).
* ``record_type`` is intentionally settable only via the owner.
"""

import uuid
from typing import Any, Dict, Optional

from pydantic import BaseModel, PrivateAttr


class PropertiesBase(BaseModel):
    record_type: str = ""  # auto-filled by Record.model_post_init
    model_config = {"extra": "allow", "frozen": False, "arbitrary_types_allowed": True}

    _owner: Optional["Record"] = PrivateAttr(default=None)  # noqa: F821

    # ------------------------------------------------------------------ #
    # binding (H1) — set by Record.model_post_init
    # ------------------------------------------------------------------ #
    def _bind(self, owner) -> None:
        object.__setattr__(self, "_owner", owner)

    # ------------------------------------------------------------------ #
    # convenience helpers — each mutation writes a new record version (H1)
    # ------------------------------------------------------------------ #
    def add(self, **kv: Any) -> None:
        """Add arbitrary key/value pairs (writes a new version)."""
        for k, v in kv.items():
            setattr(self, k, v)
        self._persist()

    def remove(self, key: str) -> None:
        """Remove a key (no error if absent) — writes a new version."""
        if hasattr(self, key):
            delattr(self, key)
            self._persist()

    def _persist(self) -> None:
        owner = self._owner
        if owner is None:
            return  # detached bag — nothing to write
        data = owner.model_dump(mode="python")
        data["properties"] = self
        data["version"] = owner.version + 1
        data["version_id"] = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"eventic:{owner.id}:{data['version']}",
            )
        )
        new_obj = owner.__class__(**data)  # type: ignore[arg-type]
        owner._commit(new_obj)

    def list(self) -> Dict[str, Any]:
        """Return all keys/values."""
        return self.model_dump()
