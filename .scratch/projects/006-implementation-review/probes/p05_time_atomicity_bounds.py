"""Regression probe: R3 (Time), R4 (atomicity boundary), R11 (verify/rebuild
memory bound).

Claims under test:
  IMPLEMENTATION_GUIDE Phase 6 "Time" row — committed_at is UTC,
      database-assigned, non-decreasing within a batch
  Phase 6 "Atomicity" row       — head upsert fails -> no log row
  docs/BENCHMARKS.md            — verify / heads rebuild: peak memory bounded
      by chunk + per-aggregate key bookkeeping, never aggregates x document

F5 (006 review): `_stream_log` folded every row into a
``dict[(stream, aggregate_id) -> full document]`` returned whole, so
`verify` and `heads rebuild` peaked at O(aggregates x document) regardless
of chunk. Fixed (007 Phase 5): the fold is now a callback that finalises a
document the moment its aggregate key changes — peak memory is one in-flight
document plus one chunk of rows (plus the orphan/head key sets in rebuild).
"""

from __future__ import annotations

from datetime import UTC
from uuid import uuid4

from pydantic import BaseModel

from eventic import App, Stream
from eventic.errors import EventicError
from eventic.ids import AggregateKey
from eventic.sql import SQLite
from eventic.sql import statements as st


class T(BaseModel):
    n: int = 0
    text: str = "x" * 200


todos = Stream(T, name="todos")

print("=== R3: committed_at precision and monotonicity ===")
store = SQLite(":memory:")
ev = App(id="d", streams=[todos]).bind(store)

a = ev[todos].create(T(n=1))
b = ev[todos].create(T(n=2))
c = ev[todos].create(T(n=3))
print(f"  three sequential commits, committed_at:")
for r in (a, b, c):
    print(f"    {r.committed_at.isoformat()}  tz={r.committed_at.tzinfo}")
print(f"  strictly increasing? {a.committed_at < b.committed_at < c.committed_at}")
print(f"  all equal?           {a.committed_at == b.committed_at == c.committed_at}")
print(f"  UTC?                 {all(r.committed_at.utcoffset().total_seconds() == 0 for r in (a, b, c))}")

with ev.batch() as batch:
    batch[todos].create(T(n=10))
    batch[todos].create(T(n=11))
page = ev[todos].where(limit=100)
in_batch = sorted({r.committed_at for r in page.items if r.state.n >= 10})
print(f"  distinct committed_at within one batch: {len(in_batch)} (batch of 2)")
print("  -> 'monotonic within a batch' holds only non-strictly; SQLite")
print("     CURRENT_TIMESTAMP is second-precision, so ordering by committed_at")
print("     cannot order revisions.")

print("\n=== R4: make the head upsert fail; does the log row survive? ===")
store2 = SQLite(":memory:")
ev2 = App(id="d", streams=[todos]).bind(store2)
first = ev2[todos].create(T(n=1))

original_upsert = store2.dialect.upsert_head
calls = {"n": 0}


def exploding_upsert(values):  # type: ignore[no-untyped-def]
    calls["n"] += 1
    raise RuntimeError("forced head-upsert failure")


object.__setattr__(store2.dialect, "upsert_head", exploding_upsert)
try:
    ev2[todos].change(first, n=2)
    outcome = "NO ERROR"
except EventicError as exc:
    outcome = f"{type(exc).__name__}: {exc}"
except Exception as exc:  # noqa: BLE001
    outcome = f"{type(exc).__name__} (NOT an EventicError): {exc}"
object.__setattr__(store2.dialect, "upsert_head", original_upsert)

print(f"  commit outcome: {outcome}")
key = AggregateKey("todos", first.id)
with store2.engine.connect() as conn:
    rows = conn.execute(st.select_window("todos", first.id, 0, 99)).mappings().all()
print(f"  log rows for the aggregate: {[r['revision'] for r in rows]}  (expect [0])")
print(f"  head revision: {store2.head(key).revision}  (expect 0)")
assert [r["revision"] for r in rows] == [0], "a log row survived a failed head upsert"
print("  -> transaction aborted, nothing written. I8 holds at this boundary.")

print("\n=== R11: verify memory is bounded by chunk, not aggregate count ===")
import tracemalloc  # noqa: E402


def peak_verify(store: SQLite, chunk: int) -> float:
    tracemalloc.start()
    store.admin().verify(None, chunk=chunk)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak / 1024


def seed(N: int) -> SQLite:
    store = SQLite(":memory:")
    ev = App(id="d", streams=[todos]).bind(store)
    for i in range(N):
        ev[todos].create(T(n=i, text="x" * 200))
    return store


small = peak_verify(seed(400), 10)
large = peak_verify(seed(400), 400)
print(f"  400 aggregates; verify peak KiB at chunk=10  : {small:.0f}")
print(f"  400 aggregates; verify peak KiB at chunk=400 : {large:.0f}")
print(f"  -> memory tracks the chunk size (the operator's knob), so the")
print(f"     chunk=10 pass stays far below materializing all 400 documents.")
assert small < large
assert small < 1024, f"verify peak at chunk=10 is {small:.0f} KiB"

print("\n=== R11: the log fold no longer materializes one doc per aggregate ===")
import inspect  # noqa: E402

from eventic.sql.admin import _stream_log  # noqa: E402

print("  _stream_log signature:", str(inspect.signature(_stream_log)))
assert "emit" in inspect.signature(_stream_log).parameters, (
    "_stream_log must be a streaming callback, not a dict-returning fold"
)
emitted: list[tuple[str, object]] = []


def record(key, doc):  # type: ignore[no-untyped-def]
    emitted.append((key, dict(doc)))


store3 = seed(400)
with store3.engine.connect() as conn:
    _stream_log(conn, store3, None, 10, emit=record)
print(f"  _stream_log(chunk=10) emitted {len(emitted)} completed documents")
assert len(emitted) == 400
print("  -> the fold emits each aggregate once; nothing is retained.")


print("\n=== unbounded listing: SqlAdmin.list_intents now pages ===")
admin = store3.admin()
import uuid as _uuid  # noqa: E402
from datetime import UTC as _UTC, datetime as _datetime  # noqa: E402
from eventic.sql.tables import eventic_intent as intents_t  # noqa: E402

base = _datetime.now(_UTC)
with store3.engine.begin() as conn:
    for i in range(5):
        conn.execute(
            intents_t.insert().values(
                intent_id=_uuid.uuid5(_uuid.NAMESPACE_URL, f"i{i}"),
                subscription_id=f"sub{i}",
                revision_id=_uuid.uuid4(),
                queue="q",
                status="pending",
                attempts=0,
                available_at=base,
                created_at=base + __import__("datetime").timedelta(seconds=i),
            )
        )
rows, cursor = admin.list_intents(limit=2)
assert len(rows) == 2 and cursor is not None
rows2, cursor2 = admin.list_intents(limit=2, cursor=cursor)
assert len(rows2) == 2 and cursor2 is not None
ids1 = {r["intent_id"] for r in rows}
ids2 = {r["intent_id"] for r in rows2}
assert not (ids1 & ids2), "pages overlap"
rows3, cursor3 = admin.list_intents(limit=2, cursor=cursor2)
assert len(rows3) == 1 and cursor3 is None
print("  list_intents(limit=2) pages 2+2+1 with an opaque cursor, no overlap.")
store3.close()
print("\nAll assertions above HELD.")
