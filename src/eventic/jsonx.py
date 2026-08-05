"""JSON primitives: the value type, canonical bytes, and the digest."""

from __future__ import annotations

import hashlib
import json
import math

type JsonValue = (
    dict[str, JsonValue] | list[JsonValue] | str | int | float | bool | None
)
type JsonObject = dict[str, JsonValue]
type JsonArray = list[JsonValue]


def canonical_bytes(tree: JsonValue) -> bytes:
    """Serialize a JSON tree canonically: sorted keys, minimal separators.

    Raises ``ValueError`` for NaN / Infinity, which cannot be written into a
    JSON column without silent drift.
    """
    if isinstance(tree, float) and not math.isfinite(tree):
        raise ValueError("non-finite float cannot be canonicalized")
    return json.dumps(
        tree,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def digest(payload: bytes) -> str:
    """sha256 hex digest of canonical bytes."""
    return hashlib.sha256(payload).hexdigest()
