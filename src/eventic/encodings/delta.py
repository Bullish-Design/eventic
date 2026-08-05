"""``delta/1``: top-level key diffs with explicit tombstones.

A checkpoint (a full ``snapshot/1`` row) is written at revision 0, every
``every`` revisions, and whenever the stream switches into delta. Removal of a
field is recorded as a tombstone in ``del`` so a removed field can never
resurrect on read (003/F4).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from eventic.jsonx import JsonObject, JsonValue


@dataclass(frozen=True)
class Delta:
    every: int = 20
    encoding_id: str = "delta/1"

    def __post_init__(self) -> None:
        if self.every < 1:
            raise ValueError("delta every must be >= 1")

    def is_checkpoint(self, revision: int) -> bool:
        return revision == 0 or revision % self.every == 0

    def encode(
        self,
        doc: JsonObject,
        *,
        base: JsonObject | None,
        base_revision: int | None,
    ) -> JsonValue:
        if base is None:
            return doc
        changed = {
            key: value
            for key, value in doc.items()
            if key not in base or base[key] != value
        }
        removed = [key for key in base if key not in doc]
        return cast(
            JsonValue,
            {
                "every": self.every,
                "base": base_revision,
                "set": changed,
                "del": removed,
            },
        )

    def decode(self, payload: JsonValue, *, base: JsonObject | None) -> JsonObject:
        if base is None:
            raise ValueError("delta payload requires a base document")
        if not isinstance(payload, dict):
            raise ValueError("delta payload is not a JSON object")
        delta_set = payload.get("set")
        delta_del = payload.get("del")
        if not isinstance(delta_set, dict):
            raise ValueError("delta payload has no 'set' object")
        if not isinstance(delta_del, list):
            raise ValueError("delta payload has no 'del' list")
        changes = cast(dict[str, JsonValue], delta_set)
        tombstones = cast(list[str], delta_del)
        result: dict[str, JsonValue] = dict(base)
        for key in tombstones:
            result.pop(key, None)
        for key, value in changes.items():
            result[key] = value
        return result
