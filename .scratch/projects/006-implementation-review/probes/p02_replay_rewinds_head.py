"""Replay detection rewinds the head to an older revision.

`SQLite._commit_one` reads the head row, then checks whether a log row already
exists at the target revision. If one does and `_is_identical` holds, it calls
`_upsert_head_from_row(conn, existing, ...)` — which upserts the head from
*that* row. Nothing compares the existing row's revision to the current head's.

So replaying an already-committed revision N while the aggregate has since
advanced to M > N overwrites the head with revision N's state.

Violates I2 (head is derived from the log and must equal the latest revision)
and CONCEPT.md §12 item 5. Reachable from ordinary at-least-once retry: the
replay path exists precisely so a client may safely re-send a commit.

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
assert head.revision == 1, f"expected the bug: head rewound, got {head.revision}"
assert head.digest != latest.digest

print("\nCONFIRMED: head rewound from revision 2 to revision 1.")
print("           head.digest != log[2].digest -> I2 violated;")
print("           ev[todos].get(id) now returns stale state 'b', not 'c'.")

# The user-visible consequence, through the public API only:
stale = ev[todos].get(r0.id)
print("\n  ev[todos].get(id).state.text =", repr(stale.state.text), "(should be 'c')")
assert stale.state.text == "b"
