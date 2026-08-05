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
from . import Plugin, Seam


class FullSnapshot(Plugin):
    """Every version stores the full state (the null codec)."""

    seam = Seam.CODEC
    provides = {"codec"}
    requires = {"persistence:json"}

    # read-hint for where(): the latest row IS the full state
    full_state_rows = True

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

    def head_state(self, persistence, class_type: str, rec_id: uuid.UUID, latest_row) -> dict:
        """The latest row's data is already the head state."""
        return latest_row.data


def _field_diff(before: dict, after: dict) -> dict:
    """Top-level fields whose JSON value actually changed."""
    return {k: v for k, v in after.items() if k not in before or before[k] != v}


def _apply_patch(state: dict, patch: dict) -> dict:
    state.update(patch)
    return state


class DiffStorage(Plugin):
    """Forward deltas + a full snapshot every ``K`` versions (the second real
    plugin — it retroactively justifies the framework, PLUGINS §8.5).

    Every ``K``-th version (and v0) stores the complete state; the others
    store only the changed top-level fields. ``decode`` reconstructs by
    replaying from the nearest snapshot, so everything above the codec seam
    (events, delivery, identity) is unaffected (CONCEPT §6).
    """

    seam = Seam.CODEC
    provides = {"codec"}
    requires = {"persistence:json"}  # incompatible with TypedTable -> caught at definition
    K = 20  # snapshot interval; tune per subclass with a ClassVar:
    # ``class Doc(Record, DiffStorage): K: ClassVar[int] = 5`` (a plain attr
    # would be picked up by pydantic as a model field)

    # read-hint for where(): the latest row may be a delta, not the state
    full_state_rows = False

    def encode(self, prev, new) -> dict:
        k = getattr(type(new), "K", self.K)  # K is tunable per subclass
        if prev is None or new.version % k == 0:
            return {"kind": "snapshot", "state": new.model_dump(mode="json")}
        return {
            "kind": "delta",
            "patch": _field_diff(prev.model_dump(mode="json"), new.model_dump(mode="json")),
        }

    def decode(self, rows: list[RecordRow]) -> dict:
        """Replay the deltas after the nearest snapshot to the last row."""
        base = next(r for r in reversed(rows) if r.data["kind"] == "snapshot")
        state = dict(base.data["state"])
        for r in rows[rows.index(base) + 1 :]:
            state = _apply_patch(state, r.data["patch"])
        return state

    def fetch(self, persistence, rec_id: uuid.UUID, class_type: str, *, version=None):
        """Read-hint: the window from the nearest snapshot forward (≤ K rows)."""
        rows = persistence.stream(rec_id, class_type)
        if version is not None:
            rows = [r for r in rows if r.version <= version]
        if not rows:
            return []
        if version is not None and rows[-1].version != version:
            return []  # exact version absent -> the caller raises KeyError
        start = max(i for i, r in enumerate(rows) if r.data["kind"] == "snapshot")
        return rows[start:]

    def head_state(self, persistence, class_type: str, rec_id: uuid.UUID, latest_row) -> dict:
        """Reconstruct the true head state (the latest row may be a delta)."""
        return self.decode(self.fetch(persistence, rec_id, class_type))
