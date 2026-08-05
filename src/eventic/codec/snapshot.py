"""``Snapshot`` — the default codec: full validated user state per version.

Each row's ``data`` *is* the complete user state, so a point read needs
exactly one row (``Window.POINT``) and ``history`` is a straight pass over the
log (F19).
"""

from __future__ import annotations

from typing import Iterator, Sequence

from ..seams import Codec, RowStore, Window
from ..state import user_state
from ..store.schema import LogRow


class Snapshot(Codec):
    requires = RowStore

    def encode(self, prev, new) -> tuple[dict, bool]:
        return user_state(new), True

    def decode(self, rows: Sequence[LogRow]) -> dict:
        """The last row already *is* the full state."""
        return rows[-1].data

    def window(self) -> Window:
        return Window.POINT

    def iter_states(self, rows: Sequence[LogRow]) -> Iterator[tuple[dict, LogRow]]:
        for row in rows:
            yield row.data, row
