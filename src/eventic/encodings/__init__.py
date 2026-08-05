"""Closed encoding registry: keyed by durable wire id, immutable."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Protocol

from eventic.errors import UndecodableRevision
from eventic.jsonx import JsonObject, JsonValue


class Encoding(Protocol):
    """A physical storage strategy for log payloads."""

    encoding_id: str

    def encode(
        self,
        doc: JsonObject,
        *,
        base: JsonObject | None,
        base_revision: int | None,
    ) -> JsonValue: ...

    def decode(self, payload: JsonValue, *, base: JsonObject | None) -> JsonObject: ...

    def is_checkpoint(self, revision: int) -> bool: ...


def get_encoding(encoding_id: str) -> Encoding:
    try:
        return ENCODINGS[encoding_id]
    except KeyError as exc:
        raise UndecodableRevision(
            f"unknown encoding id {encoding_id!r}",
        ) from exc


from eventic.encodings.delta import Delta  # noqa: E402
from eventic.encodings.snapshot import Snapshot  # noqa: E402

# Immutable registry (R9): the only module-level binding is a mapping proxy.
# A private backing dict (a previous ``_ENCODING_INSTANCES``) would be a
# module-level mutable — the exact shape the enforcement test must catch —
# so the mapping is built inline and never bound by a mutable name.
ENCODINGS: Mapping[str, Encoding] = MappingProxyType(
    {
        "snapshot/1": Snapshot(),  # type: ignore[assignment]
        "delta/1": Delta(),  # type: ignore[assignment]
    }
)
