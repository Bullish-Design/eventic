"""
Free-form metadata bag that lives on every Record instance.

* Auto-populates `record_type` with the class name.
* `add`, `remove`, `list` helpers mutate the bag in place.
* `_bind` links the bag to its owning Record so mutations can persist
  automatically (H1 — Step 5.1 wires `add`/`remove` to `_persist`).
"""

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
    # convenience helpers
    # ------------------------------------------------------------------ #
    def add(self, **kv: Any) -> None:
        """Add arbitrary key/value pairs."""
        for k, v in kv.items():
            setattr(self, k, v)

    def remove(self, key: str) -> None:
        """Remove a key (no error if absent)."""
        if hasattr(self, key):
            delattr(self, key)

    def list(self) -> Dict[str, Any]:
        """Return all keys/values."""
        return self.model_dump()
