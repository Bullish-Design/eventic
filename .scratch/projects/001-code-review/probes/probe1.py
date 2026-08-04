from __future__ import annotations
from typing import Any, Dict, Type, TypeVar
from pydantic import BaseModel

class M(BaseModel):
    id: int | None = None
    _store: ClassVar["M" | None] = None   # ClassVar NOT imported
    model_config = {"frozen": True, "extra": "allow"}
    def model_post_init(self, _ctx):
        pass

m = M()
print("M works, fields:", list(M.model_fields.keys()))
