"""Typing fixture (run under basedpyright): the README shapes type-check."""

from __future__ import annotations

from typing import Any, assert_type

from pydantic import BaseModel

from eventic import App, Outbox, Stream, Subscription
from eventic.envelopes import Commit, Page, Revision
from eventic.runtime import Collection
from eventic.sql import SQLite
from eventic.worker import Worker


class Todo(BaseModel):
    text: str
    done: bool = False


class Audit(BaseModel):
    action: str


def reindex(commit: Commit[Todo, BaseModel]) -> None:
    return None


todos = Stream(Todo, name="todos")
audits = Stream(Audit, name="audits")

app = App(
    id="todo-service",
    streams=[todos, audits],
    subscriptions=[
        Subscription(
            id="todo.reindex.v1",
            stream=todos,
            handler=reindex,
            delivery=Outbox(queue="search"),
        ),
    ],
)

ev = app.bind(SQLite(":memory:"))

assert_type(ev[todos], Collection[Todo])
t = ev[todos].create(Todo(text="learn eventic"))
assert_type(t.state, Todo)
t2 = ev[todos].change(t, done=True)
assert_type(t2, Revision[Todo, Any])


def changed_is_frozenset(commit: Commit[Todo, BaseModel]) -> frozenset[str]:
    return commit.changed


page: Page[Revision[Todo, BaseModel]] = ev[todos].history(t.id)
assert_type(page.items[0].state, Todo)


def worker_types() -> None:
    worker = Worker(app, ev.store, queue="search")
    report = worker.drain_once()
    assert_type(report.claimed, int)
