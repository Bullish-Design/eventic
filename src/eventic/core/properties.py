"""
Free-form metadata bag that lives on every Record instance.

* Auto-populates `record_type` with the class name.
* `add`, `remove`, `list` helpers mutate the bag in place.
"""

from typing import Any, Dict

from pydantic import BaseModel


class PropertiesBase(BaseModel):
    record_type: str = ""  # auto-filled by Record.model_post_init
    model_config = {"extra": "allow", "frozen": False, "arbitrary_types_allowed": True}

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
