"""delta/1: reserved keys (R6), point-read bounds (R9), rebuild exactness (R2).

Claims under test:
  ARCHITECTURE.md §5.2 — reconstruction reads a bounded window, "at most K rows"
  docs/BENCHMARKS.md   — get(id, revision=n) is <= K+1 rows for delta/1
  CONCEPT.md §12 (5,6) — heads byte-exactly rebuildable, no orphan
  §3.4 R6             — a user document whose top-level keys are set/del/base/every

Regression probe: the 006 review refuted these candidates (delta is correct)
and F8 folded the rebuild-exactness claim into the four-way property test;
this probe keeps the direct, hand-shaped assertions alive.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from sqlalchemy import event as sa_event

from eventic import App, Stream
from eventic.encodings.delta import Delta
from eventic.ids import AggregateKey
from eventic.sql import SQLite


class Doc(BaseModel):
    """Top-level field names deliberately collide with the delta envelope."""

    set: int = 0
    dele: str = ""  # `del` is a Python keyword; aliased below
    base: int = 0
    every: str = ""
    payload: str = ""

    model_config = {"populate_by_name": True}


class Plain(BaseModel):
    n: int = 0
    text: str = ""


def counting(store: SQLite) -> list[str]:
    seen: list[str] = []

    @sa_event.listens_for(store.engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):  # type: ignore[no-untyped-def]
        seen.append(" ".join(statement.split())[:70])

    return seen


print("=== R6: reserved delta keys ===")
docs = Stream(Doc, name="docs")
store = SQLite(":memory:", encodings={"docs": Delta(every=5)})
ev = App(id="d", streams=[docs]).bind(store)

r = ev[docs].create(Doc(set=1, dele="a", base=2, every="e", payload="p"))
for i in range(1, 8):
    r = ev[docs].change(r, set=i, base=i * 10, every=f"e{i}", payload=f"p{i}")

key = AggregateKey("docs", r.id)
ok = True
for n in range(0, 8):
    got = store.revision(key, n)
    assert got is not None
    live = ev[docs].get(r.id, revision=n)
    exact = got.digest == live.digest
    ok = ok and exact
print(f"  8 revisions of a doc with keys set/base/every: all digests exact -> {ok}")
head = store.head(key)
assert head is not None
assert head.digest == r.digest
print(f"  head digest == last returned revision digest              -> True")
print(f"  final state: {store.revision(key, 7).payload}")
assert ok

print("\n=== R9 / BENCHMARKS: point read at revision n touches <= K+1 rows ===")
plains = Stream(Plain, name="plains")
K = 20
store2 = SQLite(":memory:", encodings={"plains": Delta(every=K)})
ev2 = App(id="d", streams=[plains]).bind(store2)
p = ev2[plains].create(Plain(n=0, text="t0"))
for i in range(1, 61):
    p = ev2[plains].change(p, n=i, text=f"t{i}")

key2 = AggregateKey("plains", p.id)
seen = counting(store2)
seen.clear()
got = store2.revision(key2, 47)
selects = [s for s in seen if s.upper().startswith("SELECT")]
print(f"  statements issued for revision(47): {len(seen)}")
for s in seen:
    print(f"    {s}")

# how many log rows did that window actually span?
import sqlalchemy as sa  # noqa: E402

from eventic.sql import statements as st  # noqa: E402

with store2.engine.connect() as conn:
    rows = conn.execute(st.select_window("plains", p.id, 40, 47)).mappings().all()
print(f"  window rows for [40..47]: {len(rows)}  (K+1 = {K + 1})")

print("\n=== R2 fifth leg: rebuild_heads under delta is byte-exact ===")
admin = store2.admin()
before = store2.head(key2)
assert before is not None
report = admin.rebuild_heads(None, chunk=10)
after = store2.head(key2)
assert after is not None
print(f"  rebuild report: {report}")
print(f"  head digest before : {before.digest}")
print(f"  head digest after  : {after.digest}")
print(f"  byte-exact         : {before.digest == after.digest}")
assert before.digest == after.digest, "rebuild diverged under delta"
assert after.revision == before.revision

print("\n=== rebuild removes orphan heads ===")
from eventic.sql.tables import eventic_head  # noqa: E402
from uuid import uuid4  # noqa: E402

orphan = uuid4()
with store2.engine.begin() as conn:
    conn.execute(
        eventic_head.insert().values(
            stream="plains",
            aggregate_id=orphan,
            revision=0,
            revision_id=uuid4(),
            schema_version=1,
            meta_version=1,
            state={"n": 0, "text": "ghost"},
            digest="0" * 64,
            meta={},
            committed_at=before.committed_at,
        )
    )
rep2 = admin.rebuild_heads(None, chunk=10)
gone = store2.head(AggregateKey("plains", orphan))
print(f"  rebuild report: {rep2}")
print(f"  orphan head after rebuild: {gone}")
assert gone is None
print("\nAll delta assertions above HELD.")
