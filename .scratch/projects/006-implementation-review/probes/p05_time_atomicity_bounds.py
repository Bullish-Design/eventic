"""R3 (Time), R4 (atomicity boundary), R11 (verify/rebuild memory bound).

Claims under test:
  IMPLEMENTATION_GUIDE Phase 6 "Time" row — committed_at is UTC,
      database-assigned, monotonic within a batch
  Phase 6 "Atomicity" row       — head upsert fails -> no log row
  docs/BENCHMARKS.md            — verify / heads rebuild: "bounded memory per
      chunk"; "nothing materializes an unbounded result"
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

print("\n=== R11: does verify/rebuild bound memory per chunk? ===")
import tracemalloc  # noqa: E402

store3 = SQLite(":memory:")
ev3 = App(id="d", streams=[todos]).bind(store3)
N = 400
for i in range(N):
    ev3[todos].create(T(n=i))
admin = store3.admin()


def peak_for(chunk: int) -> float:
    tracemalloc.start()
    admin.verify(None, chunk=chunk)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak / 1024


small = peak_for(10)
large = peak_for(N)
print(f"  {N} aggregates; verify peak KiB at chunk=10  : {small:.0f}")
print(f"  {N} aggregates; verify peak KiB at chunk={N} : {large:.0f}")
print("  If memory were bounded per chunk these would differ ~40x.")

from eventic.sql.admin import _stream_log  # noqa: E402

with store3.engine.connect() as conn:
    folded = _stream_log(conn, store3, None, 10)
print(f"  _stream_log(chunk=10) returned {len(folded)} fully-materialized documents")
print(f"  -> one entry per aggregate in scope, regardless of chunk. Memory is")
print(f"     O(aggregates x document), NOT bounded per chunk.")
assert len(folded) == N

print("\n=== unbounded listing: SqlAdmin.list_intents ===")
import inspect  # noqa: E402

src = inspect.getsource(admin.__class__.list_intents)
print("  " + "\n  ".join(src.strip().splitlines()))
print("  -> no limit / offset / cursor: the whole intent table is materialized.")
