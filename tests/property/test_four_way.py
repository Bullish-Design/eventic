"""Phase 8: runtime, dispatch, and the four-way agreement property.

The highest-leverage test in the suite: for any command sequence,
``digest(head) == digest(replay(log)) == digest(history[-1]) ==
digest(returned Revision)``.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import settings
from hypothesis.stateful import Bundle, RuleBasedStateMachine, invariant, rule
from hypothesis.strategies import booleans, text
from pydantic import BaseModel, ValidationError

from eventic.app import App
from eventic.envelopes import Commit, Revision
from eventic.errors import InlineDispatchError, RevisionConflict, StoreError, UsageError
from eventic.ids import AggregateKey
from eventic.runtime import Runtime
from eventic.sql.store import SQLite
from eventic.stream import Stream
from eventic.subscription import Inline, Subscription


class Todo(BaseModel):
    text: str
    done: bool = False
    tags: list[str] = []


class Audit(BaseModel):
    action: str


def _app() -> App:
    todos = Stream(Todo, name="todos")
    audits = Stream(Audit, name="audits")
    return App(id="demo", streams=[todos, audits])


class FourWayMachine(RuleBasedStateMachine):
    """Every committed revision satisfies head == replay == history[-1] == returned."""

    revisions = Bundle("revisions")

    def __init__(self) -> None:
        super().__init__()
        self.store = SQLite(":memory:")
        self.runtime: Runtime = _app().bind(self.store)
        self.todos = self.runtime._app.streams[0]
        self.audits = self.runtime._app.streams[1]
        self.known: list[Revision[Any, Any]] = []

    def _check(self, revision: Revision[Any, Any]) -> None:
        key = AggregateKey(revision.stream, revision.id)
        head = self.store.head(key)
        assert head is not None
        assert head.digest == revision.digest
        replayed = self.store.revision(key, revision.revision)
        assert replayed is not None
        assert replayed.digest == revision.digest
        page = self.store.history(key, after=-1, limit=1000)
        assert page.items
        assert page.items[-1].digest == revision.digest

    @invariant()
    def heads_are_stable(self) -> None:
        for revision in self.known:
            head = self.store.head(AggregateKey(revision.stream, revision.id))
            assert head is not None
            assert head.digest == revision.digest
            assert head.revision == revision.revision

    @rule(text=text(alphabet="abc", max_size=5))
    def create_todo(self, text: str) -> None:
        revision = self.runtime[self.todos].create(Todo(text=text))
        self.known.append(revision)
        self._check(revision)

    @rule(action=text(alphabet="xyz", max_size=5))
    def create_audit(self, action: str) -> None:
        revision = self.runtime[self.audits].create(Audit(action=action))
        self.known.append(revision)
        self._check(revision)

    @rule(done=booleans(), old=revisions, target=revisions)
    def change_todo(self, done: bool, old: Revision[Any, Any]) -> Revision[Any, Any]:
        try:
            new = self.runtime[self.todos].change(old, done=done)
        except RevisionConflict:
            return old  # a stale handle is rejected loudly; nothing durable changes
        self._replace_known(old, new)
        self._check(new)
        return new

    @rule(text=text(alphabet="abc", max_size=5), old=revisions, target=revisions)
    def replace_todo(self, text: str, old: Revision[Any, Any]) -> Revision[Any, Any]:
        try:
            new = self.runtime[self.todos].replace(
                old, Todo(text=text, done=old.state.done)
            )
        except RevisionConflict:
            return old
        self._replace_known(old, new)
        self._check(new)
        return new

    @rule(old=revisions)
    def mutate_nested_list(self, old: Revision[Any, Any]) -> None:
        # Mutating the returned state's nested list must change nothing durable.
        old.state.tags.append("sneaky")
        head = self.store.head(AggregateKey(old.stream, old.id))
        assert head is not None
        assert head.digest == old.digest
        assert head.payload["tags"] == []

    @rule(old=revisions, target=revisions)
    def batch_change(self, old: Revision[Any, Any]) -> Revision[Any, Any]:
        try:
            with self.runtime.batch() as batch:
                batch[self.todos].change(old, done=True)
                batch[self.audits].create(Audit(action="batched"))
        except RevisionConflict:
            return old
        head = self.store.head(AggregateKey(old.stream, old.id))
        assert head is not None
        assert head.revision == old.revision + 1
        new = self.store.revision(AggregateKey(old.stream, old.id), head.revision)
        assert new is not None
        from eventic.hydration import hydrate
        from eventic.meta import NoMeta

        hydrated = hydrate(
            self.todos if new.stream == self.todos.name else self.audits,
            NoMeta,
            new,
        )
        self._replace_known(old, hydrated)
        self._check(hydrated)
        return hydrated

    def _replace_known(self, old: Revision[Any, Any], new: Revision[Any, Any]) -> None:
        for idx, revision in enumerate(self.known):
            if revision.stream == old.stream and revision.id == old.id:
                self.known[idx] = new
                return
        self.known.append(new)

    def teardown(self) -> None:
        self.store.close()


FourWayMachine.TestCase.settings = settings(max_examples=300, deadline=None)


@pytest.mark.filterwarnings("ignore::ResourceWarning")
def test_four_way_agreement() -> None:
    from hypothesis.stateful import run_state_machine_as_test

    run_state_machine_as_test(FourWayMachine)


# ---------------------------------------------------------------------------
# Dispatch semantics
# ---------------------------------------------------------------------------


def _todo_app(handlers: list[Subscription[Any, Any]]) -> App:
    todos = Stream(Todo, name="todos")
    return App(id="demo", streams=[todos], subscriptions=tuple(handlers))


def test_inline_handlers_run_in_declaration_order() -> None:
    store = SQLite(":memory:")
    todos = Stream(Todo, name="todos")
    order: list[str] = []

    def first(commit: Commit[Todo, BaseModel]) -> None:
        order.append("first")

    def second(commit: Commit[Todo, BaseModel]) -> None:
        order.append("second")

    runtime = _todo_app(
        [
            Subscription(id="a", stream=todos, handler=first, delivery=Inline()),
            Subscription(id="b", stream=todos, handler=second, delivery=Inline()),
        ]
    ).bind(store)
    runtime[todos].create(Todo(text="x"))
    assert order == ["first", "second"]
    store.close()


def test_one_failing_handler_does_not_block_others() -> None:
    store = SQLite(":memory:")
    todos = Stream(Todo, name="todos")
    order: list[str] = []

    def boom(commit: Commit[Todo, BaseModel]) -> None:
        order.append("boom")
        raise RuntimeError("handler failed")

    def fine(commit: Commit[Todo, BaseModel]) -> None:
        order.append("fine")

    runtime = _todo_app(
        [
            Subscription(id="a", stream=todos, handler=boom),
            Subscription(id="b", stream=todos, handler=fine),
        ]
    ).bind(store)
    with pytest.raises(InlineDispatchError) as excinfo:
        runtime[todos].create(Todo(text="x"))
    assert order == ["boom", "fine"]
    assert "handler failed" in str(excinfo.value)
    assert "subscription a" in str(excinfo.value)
    store.close()


def test_inline_handlers_not_called_when_commit_fails() -> None:
    store = SQLite(":memory:")
    todos = Stream(Todo, name="todos")
    calls: list[str] = []

    def handler(commit: Commit[Todo, BaseModel]) -> None:
        calls.append("called")

    class FailingStore:
        def __init__(self, inner: SQLite) -> None:
            self._inner = inner
            self.capabilities = inner.capabilities

        def commit(self, requests):
            self._inner.commit(requests)  # durable
            raise StoreError("forced failure after the log insert")

        def head(self, key):
            return self._inner.head(key)

        def revision(self, key, revision):
            return self._inner.revision(key, revision)

        def history(self, key, *, after, limit):
            return self._inner.history(key, after=after, limit=limit)

        def search(self, stream, filters, *, cursor, limit):
            return self._inner.search(stream, filters, cursor=cursor, limit=limit)

        def claim(self, queue, *, limit, lease):
            return self._inner.claim(queue, limit=limit, lease=lease)

        def settle(self, settlements):
            return self._inner.settle(settlements)

    runtime = _todo_app([Subscription(id="a", stream=todos, handler=handler)]).bind(
        FailingStore(store)
    )
    with pytest.raises(StoreError):
        runtime[todos].create(Todo(text="x"))
    assert calls == []
    store.close()


def test_batch_has_no_reads() -> None:
    store = SQLite(":memory:")
    runtime = _todo_app([]).bind(store)
    todos = runtime._app.streams[0]
    collection = runtime.batch()[todos]
    assert not hasattr(collection, "get")
    assert not hasattr(collection, "history")
    assert not hasattr(collection, "where")
    store.close()


def test_batch_all_or_nothing() -> None:
    store = SQLite(":memory:")
    todos = Stream(Todo, name="todos")
    audits = Stream(Audit, name="audits")
    app = App(id="demo", streams=[todos, audits])
    runtime = app.bind(store)

    first = runtime[todos].create(Todo(text="a"))
    with pytest.raises(ValidationError), runtime.batch() as batch:
        batch[todos].change(first, done=True)
        batch[audits].change(first, done=True)  # wrong stream type -> plan error
    head = store.head(AggregateKey("todos", first.id))
    assert head is not None
    assert head.revision == 0
    store.close()


def test_vertical_slice() -> None:
    """The Phase 8 exit gate, in a fresh process shape."""
    todos = Stream(Todo, name="todos")
    ev = App(id="demo", streams=[todos]).bind(SQLite(":memory:"))
    t = ev[todos].create(Todo(text="a"))
    t = ev[todos].change(t, done=True)
    assert ev[todos].get(t.id).digest == t.digest
    assert [r.revision for r in ev[todos].history(t.id).items] == [0, 1]
    ev._store.close()


def test_collection_unknown_stream_rejected() -> None:
    store = SQLite(":memory:")
    others = Stream(Todo, name="others")
    runtime = _todo_app([]).bind(store)
    with pytest.raises(UsageError):
        runtime[others]
    store.close()


def test_where_equality() -> None:
    store = SQLite(":memory:")
    todos = Stream(Todo, name="todos")
    runtime = _todo_app([]).bind(store)
    first = runtime[todos].create(Todo(text="alpha", done=False))
    runtime[todos].create(Todo(text="beta", done=True))
    page = runtime[todos].where(done=True)
    assert [r.id for r in page.items] == [page.items[0].id]
    assert page.items[0].state.text == "beta"
    page2 = runtime[todos].where(text="alpha")
    assert [r.id for r in page2.items] == [first.id]
    store.close()
