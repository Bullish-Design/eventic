"""Regression probe: replay of a superseded revision must not rewind the head.

F1 (006 review): `SQLite._commit_one` used to upsert the head from whatever
log row matched the replay target, so re-sending an already-committed revision
N while the aggregate had advanced to M > N rewound the head to N's state.
That violated I2 (head derived from the log) and made at-least-once retries
silently corrupt reads.

Fixed (007 Phase 1): the head write is guarded — it happens only when the head
is missing (repair) or the replayed row is newer than the head. A superseded
replay now returns `replayed=True` and leaves the head alone.

Run: devenv shell -- uv run python .scratch/.../probes/p02_replay_rewinds_head.py
"""

from __future__ import annotations

from pydantic import BaseModel

from eventic import App, Stream
from eventic.ids import AggregateKey
from eventic.planning import plan_change
from eventic.sql import SQLite


class Todo(BaseModel):
    text: str
    done: bool = False


todos = Stream(Todo, name="todos")
app = App(id="d", streams=[todos])
store = SQLite(":memory:")
ev = app.bind(store)

r0 = ev[todos].create(Todo(text="a"))
r1 = ev[todos].change(r0, text="b")
r2 = ev[todos].change(r1, text="c")

key = AggregateKey("todos", r0.id)
print("after three commits:")
print("  head revision :", store.head(key).revision)
print("  head state    :", store.head(key).payload)
assert store.head(key).revision == 2

# Re-send the revision-1 commit verbatim: same base, same fields, so the
# canonical payload and digest are byte-identical to what is already stored.
replayed_request = plan_change(app, todos, r0, {"text": "b"})
results = store.commit([replayed_request])

print("\nreplay of revision 1 -> replayed =", results[0].replayed)
print("  head revision :", store.head(key).revision)
print("  head state    :", store.head(key).payload)
print("  log latest    :", store.revision(key, 2).revision, store.revision(key, 2).payload)

head = store.head(key)
latest = store.revision(key, 2)
assert results[0].replayed is True
assert head.revision == 2, f"head rewound to {head.revision}"
assert head.digest == latest.digest

print("\nOK: head stayed at revision 2; head.digest == log[2].digest -> I2 holds.")

# The user-visible consequence, through the public API only:
fresh = ev[todos].get(r0.id)
print("  ev[todos].get(id).state.text =", repr(fresh.state.text), "(stays 'c')")
assert fresh.state.text == "c"
print("\nREGRESSION PROBE PASSED: superseded replay is a no-op on the head.")
store.close()
