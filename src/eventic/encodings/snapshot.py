"""``snapshot/1``: the canonical document verbatim. The default encoding."""

from __future__ import annotations

from dataclasses import dataclass

from eventic.jsonx import JsonObject, JsonValue


@dataclass(frozen=True)
class Snapshot:
    encoding_id: str = "snapshot/1"

    def encode(
        self,
        doc: JsonObject,
        *,
        base: JsonObject | None,
        base_revision: int | None,
    ) -> JsonValue:
        return doc

    def decode(self, payload: JsonValue, *, base: JsonObject | None) -> JsonObject:
        if not isinstance(payload, dict):
            raise ValueError("snapshot payload is not a JSON object")
        return payload

    def is_checkpoint(self, revision: int) -> bool:
        return True
