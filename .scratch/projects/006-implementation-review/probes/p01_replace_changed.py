"""R1 — Collection.replace passes the NEW state as the "before" document.

Expected per ARCHITECTURE.md §2.2: `changed` is "top-level keys whose canonical
value differs", and an inline handler and a worker rebuilding the same commit
from the log must receive field-for-field identical envelopes (§10 / 004/F10).

Run: .venv/bin/python .scratch/projects/006-implementation-review/probes/p01_replace_changed.py
"""

from __future__ import annotations

from pydantic import BaseModel

from eventic import App, Stream, Subscription
from eventic.envelopes import Commit
from eventic.sql import SQLite


class Todo(BaseModel):
    text: str
    done: bool = False


seen: list[Commit[Todo, BaseModel]] = []


def handler(c: Commit[Todo, BaseModel]) -> None:
    seen.append(c)


todos = Stream(Todo, name="todos")
app = App(
    id="d",
    streams=[todos],
    subscriptions=[Subscription(id="i", stream=todos, handler=handler)],
)
ev = app.bind(SQLite(":memory:"))

t = ev[todos].create(Todo(text="a"))
t2 = ev[todos].replace(t, Todo(text="b", done=True))

print("create changed :", sorted(seen[0].changed))
print("replace changed:", sorted(seen[1].changed))
print("state before   :", t.state.model_dump())
print("state after    :", t2.state.model_dump())

# What the worker reconstructs for the same commit: diff of the two logical
# documents in the log.
from eventic.planning import changed_keys, state_tree  # noqa: E402

durable = changed_keys(state_tree(todos, t.state), state_tree(todos, t2.state))
print("worker changed :", sorted(durable))

assert seen[1].changed == frozenset(), "expected the bug: inline replace changed is empty"
assert durable == {"text", "done"}, durable
print("\nCONFIRMED: inline replace envelope reports changed=frozenset(),")
print("           durable reconstruction reports {'done', 'text'}.")
