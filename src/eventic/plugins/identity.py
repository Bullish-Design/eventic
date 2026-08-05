"""Identity seam — deterministic ``version_id`` derivation (I4).

Default provider: ``Uuid5Deterministic`` — ``uuid5(NAMESPACE_URL,
"eventic:{id}:{version}")`` for **every** version including v0 (closes R-C2).
Exclusive seam. This module is a leaf: ``record.py`` imports ``_uuid5`` from
here (no import cycle).
"""

from __future__ import annotations

import uuid


def _uuid5(id: uuid.UUID, version: int) -> uuid.UUID:
    """Deterministic version identity (I4): the same (id, version) always
    yields the same version_id — for replays, retries, and v0."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"eventic:{id}:{version}")


class Uuid5Deterministic:
    """The one true identity rule (I4)."""

    provides = {"identity"}

    def version_id_for(self, id: uuid.UUID, version: int) -> uuid.UUID:
        return _uuid5(id, version)
