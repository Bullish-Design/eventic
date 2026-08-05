"""Replay semantics: re-sending an already-committed write is a no-op on the
head, and a superseded replay must never rewind it (I2)."""

from __future__ import annotations

from pydantic import BaseModel

from eventic.app import App
from eventic.ids import AggregateKey
from eventic.planning import plan_change
from eventic.sql.store import SQLite
from eventic.stream import Stream


class Todo(BaseModel):
    text: str
    done: bool = False


def test_replay_of_superseded_revision_leaves_head_alone() -> None:
    """Replay of revision 1 after the head moved to 2 must not rewind it.

    The replay path exists so an at-least-once caller may safely re-send a
    commit whose ack was lost. Re-sending an *older* revision is the same
    retry; the head must stay where the log says it is.
    """
    store = SQLite(":memory:")
    todos = Stream(Todo, name="todos")
    app = App(id="demo", streams=[todos])
    runtime = app.bind(store)

    r0 = runtime[todos].create(Todo(text="a"))
    r1 = runtime[todos].change(r0, text="b")
    runtime[todos].change(r1, text="c")
    key = AggregateKey("todos", r0.id)
    assert store.head(key).revision == 2
    assert store.head(key).payload["text"] == "c"

    # Re-send the revision-1 write verbatim: same base, same fields, so the
    # canonical payload and digest match what is already in the log.
    replay = plan_change(app, todos, r0, {"text": "b"})
    result = store.commit([replay])[0]

    head = store.head(key)
    latest = store.revision(key, 2)
    assert result.replayed is True
    assert head is not None
    assert latest is not None
    assert head.revision == 2, f"head rewound to {head.revision}"
    assert head.digest == latest.digest
    assert head.payload == latest.payload
    # the log itself is untouched: exactly three rows
    page = store.history(key, after=-1, limit=100)
    assert [r.revision for r in page.items] == [0, 1, 2]
    store.close()


def test_replay_of_current_revision_is_still_a_noop() -> None:
    """Replaying the head revision keeps the head where it is."""
    store = SQLite(":memory:")
    todos = Stream(Todo, name="todos")
    app = App(id="demo", streams=[todos])
    runtime = app.bind(store)

    r0 = runtime[todos].create(Todo(text="a"))
    r1 = runtime[todos].change(r0, text="b")
    replay = plan_change(app, todos, r0, {"text": "b"})
    result = store.commit([replay])[0]
    assert result.replayed is True
    key = AggregateKey("todos", r0.id)
    head = store.head(key)
    assert head.revision == 1
    assert head.digest == r1.digest
    store.close()


def test_replay_repairs_a_missing_head() -> None:
    """The head may be deleted (or never built); a replay restores it from the
    log without appending anything (test_admin depends on this repair)."""
    store = SQLite(":memory:")
    todos = Stream(Todo, name="todos")
    app = App(id="demo", streams=[todos])
    runtime = app.bind(store)

    r0 = runtime[todos].create(Todo(text="a"))
    key = AggregateKey("todos", r0.id)
    # delete the head behind the store's back; the log row survives
    from sqlalchemy import text

    with store.engine.begin() as conn:
        conn.execute(text("DELETE FROM eventic_head"))
    assert store.head(key) is None

    # re-send the create verbatim: the log row at revision 0 exists, so this
    # is a replay, and the missing head is rebuilt from that row
    from eventic.planning import plan_create

    replay = plan_create(app, todos, Todo(text="a"), r0.id)
    result = store.commit([replay])[0]
    assert result.replayed is True
    head = store.head(key)
    assert head is not None
    assert head.revision == 0
    assert head.digest == r0.digest
    store.close()
