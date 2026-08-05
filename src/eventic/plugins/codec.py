"""Codec seam — how a version's state is represented in a row.

Default provider: ``FullSnapshot`` — each version row stores the complete
validated model dump. ``decode(rows)`` reconstructs the state as of the last
row; ``fetch`` is the codec's read-hint telling the pipeline which row window
it needs (the default only needs the target row, a diff codec needs the whole
prefix back to its nearest snapshot). Exclusive seam.
"""

from __future__ import annotations

import uuid

from ..models import RecordRow


class FullSnapshot:
    """Every version stores the full state (the null codec)."""

    provides = {"codec"}
    requires = {"persistence:json"}

    def encode(self, prev, new) -> dict:
        return new.model_dump(mode="json")

    def decode(self, rows: list[RecordRow]) -> dict:
        """The last row already *is* the full state."""
        return rows[-1].data

    def fetch(self, persistence, rec_id: uuid.UUID, class_type: str, *, version=None):
        """Read-hint: one row suffices for a full snapshot."""
        row = (
            persistence.at(rec_id, version, class_type)
            if version is not None
            else persistence.latest(rec_id, class_type)
        )
        return [row] if row is not None else []
