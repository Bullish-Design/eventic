"""Deterministic version identity (I4).

``version_id(id, version)`` is **the one true identity rule**: a module
function, not a seam (F9 — v2's identity seam was never called by anything).
The same ``(id, version)`` always yields the same ``version_id`` — for
replays, retries, and v0.
"""

from __future__ import annotations

import uuid

NS = uuid.NAMESPACE_URL


def version_id(id: uuid.UUID, version: int) -> uuid.UUID:
    """``uuid5(NS, "eventic:{id}:{version}")`` for every version incl. v0."""
    return uuid.uuid5(NS, f"eventic:{id}:{version}")
