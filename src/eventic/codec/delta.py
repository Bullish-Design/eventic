"""``Delta`` — forward deltas + tombstones + a snapshot every ``k`` versions.

A snapshot row's ``data`` is the full user state; a delta row stores
``{"set": {...}, "del": [...]}`` — changes **and** tombstones, so a removed
field cannot resurrect on read (F4). ``requires = JsonRowStore``: this codec
can only be paired with a JSON-shaped store, checked at class definition.

A point read reaches back to the nearest snapshot — at most ``k`` rows,
regardless of how long the aggregate has lived (F17). ``k`` is per-class:
``class Doc(Record, codec=Delta(k=20))``.
"""

from __future__ import annotations

from typing import Iterator, Sequence

from ..seams import Codec, JsonRowStore, Window
from ..state import user_state
from ..store.schema import LogRow

_MISS = object()


class Delta(Codec):
    requires = JsonRowStore

    def __init__(self, *, k: int = 20):
        if k < 1:
            raise ValueError("Delta(k=...) must be >= 1")
        self.k = k

    # ------------------------------------------------------------------ #
    def encode(self, prev, new) -> tuple[dict, bool]:
        after = user_state(new)
        if prev is None or new.version % self.k == 0:
            return after, True
        before = user_state(prev)
        changed = {
            k: v
            for k, v in after.items()
            if before.get(k, _MISS) != v
        }
        removed = sorted(before.keys() - after.keys())
        return {"set": changed, "del": removed}, False

    def decode(self, rows: Sequence[LogRow]) -> dict:
        """Fold forward from the nearest snapshot to the last row."""
        state = dict(rows[0].data)
        for row in rows[1:]:
            state = _apply(state, row.data)
        return state

    def window(self) -> Window:
        return Window.SINCE_SNAPSHOT

    def iter_states(self, rows: Sequence[LogRow]) -> Iterator[tuple[dict, LogRow]]:
        state: dict = {}
        for row in rows:
            if row.snapshot:
                state = dict(row.data)
            else:
                state = _apply(state, row.data)
            yield state, row


def _apply(state: dict, patch: dict) -> dict:
    state.update(patch.get("set", {}))
    for key in patch.get("del", []):
        state.pop(key, None)
    return state
