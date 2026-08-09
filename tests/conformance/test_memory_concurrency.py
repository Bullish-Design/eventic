"""Concurrent writes on ``SQLite(\":memory:\")`` are serialized.

The in-memory store shares a single DBAPI connection across threads
(StaticPool + check_same_thread=False). Concurrent operations used to
interleave BEGIN/INSERT/COMMIT on that one connection: writes were lost and
commits raised ``StoreError('commit failed')``. The pool now holds an RLock
for the whole checkout, so each operation owns the connection exclusively.
"""

from __future__ import annotations

import threading

from pydantic import BaseModel

from eventic import App, Stream
from eventic.sql import SQLite


class Probe(BaseModel):
    path: str = ""


PROBE_STREAM = Stream(Probe, name="conc")


def test_concurrent_creates_on_memory_store_lose_nothing() -> None:
    store = SQLite(":memory:")
    try:
        runtime = App(id="t", streams=[PROBE_STREAM]).bind(store)
        col = runtime[PROBE_STREAM]
        errors: list[Exception] = []

        def worker(prefix: str) -> None:
            for i in range(50):
                try:
                    col.create(Probe(path=f"{prefix}-{i}"))
                except Exception as e:  # noqa: BLE001 - collect, don't fail the thread
                    errors.append(e)

        threads = [
            threading.Thread(target=worker, args=(f"t{t}",)) for t in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(col.where(limit=1000).items) == 200
    finally:
        store.close()
