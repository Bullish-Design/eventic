"""Regression probe: `replace` reports the same `changed` set inline and durable.

F3 (006 review, candidate R1 confirmed): `Collection.replace` passed the NEW
state as the ``before`` argument to ``_commit_one``, so ``_changed`` diffed the
new document against itself and always yielded ``frozenset()``. The worker
reconstructs the true diff from the log, so the two envelopes for the same
commit disagreed — the exact property ARCHITECTURE.md §2.2 says is guaranteed
("field-for-field identical envelopes").

Fixed (007 Phase 3): `replace` passes ``base.state``, exactly as `change`
already did. Same fix in `BatchCollection.replace`.

Run: devenv shell -- uv run python .scratch/.../probes/p01_replace_changed.py
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

assert seen[1].changed == durable, (
    f"inline replace changed {sorted(seen[1].changed)} != "
    f"durable {sorted(durable)}"
)
assert durable == {"text", "done"}, durable
print("\nOK: inline replace changed == durable changed == {'done', 'text'}.")
print("    Inline and worker envelopes agree for create, change AND replace.")
