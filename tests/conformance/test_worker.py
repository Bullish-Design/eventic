"""Phase 10: delivery — the worker, retry/dead-letter, envelope equality."""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

import pytest
from pydantic import BaseModel

from eventic.app import App
from eventic.envelopes import Commit
from eventic.errors import DeadLettered, StoreError
from eventic.runtime import Runtime
from eventic.sql.store import SQLite
from eventic.stream import Stream
from eventic.subscription import Backoff, Outbox, Subscription
from eventic.worker import Worker, WorkerReport


class Todo(BaseModel):
    text: str
    done: bool = False


def _app(
    todos: Stream[Todo],
    handler: Any,
    *,
    queue: str = "q",
    max_attempts: int = 3,
) -> App:
    return App(
        id="demo",
        streams=[todos],
        subscriptions=[
            Subscription(
                id="sub.a",
                stream=todos,
                handler=handler,
                delivery=Outbox(
                    queue=queue,
                    retry=Backoff(
                        max_attempts=max_attempts, base=0.01, factor=1.0, cap=0.02
                    ),
                ),
            )
        ],
    )


def _seed(store: SQLite, app: App, text: str = "hi") -> Runtime:
    todos = app.streams[0]
    runtime = app.bind(store)
    runtime[todos].create(Todo(text=text))
    return runtime


def test_deliver_and_ack() -> None:
    store = SQLite(":memory:")
    todos = Stream(Todo, name="todos")
    seen: list[Commit[Todo, BaseModel]] = []

    def handler(commit: Commit[Todo, BaseModel]) -> None:
        seen.append(commit)

    app = _app(todos, handler)
    _seed(store, app)
    worker = Worker(app, store, queue="q")
    report = worker.drain_once()
    assert report.claimed == 1
    assert report.delivered == 1
    assert len(seen) == 1
    assert seen[0].revision.state.text == "hi"
    assert seen[0].kind == "create"
    assert seen[0].changed == frozenset({"text", "done"})
    # ack deleted the intent; queue is drained
    report2 = worker.drain_once()
    assert report2.claimed == 0
    store.close()


def test_retry_then_deliver() -> None:
    store = SQLite(":memory:")
    todos = Stream(Todo, name="todos")
    attempts: list[int] = []

    def flaky(commit: Commit[Todo, BaseModel]) -> None:
        attempts.append(len(attempts))
        if len(attempts) == 1:
            raise RuntimeError("first try fails")

    app = _app(todos, flaky)
    _seed(store, app)
    worker = Worker(app, store, queue="q")
    report = worker.drain_once()
    assert report.retried == 1
    assert report.dead_lettered == 0
    # the retry is scheduled in the future (base=0.01s); wait and drain again
    time.sleep(0.03)
    report2 = worker.drain_once()
    assert report2.delivered == 1
    assert len(attempts) == 2
    store.close()


def test_retry_exhaustion_dead_letters() -> None:
    store = SQLite(":memory:")
    todos = Stream(Todo, name="todos")

    def always_fails(commit: Commit[Todo, BaseModel]) -> None:
        raise RuntimeError("nope")

    app = _app(todos, always_fails, max_attempts=2)
    _seed(store, app)
    worker = Worker(app, store, queue="q")
    report = worker.drain_once()
    assert report.retried == 1
    time.sleep(0.03)
    report2 = worker.drain_once()
    assert report2.dead_lettered == 1
    assert report2.delivered == 0
    # dead intents are not claimable
    report3 = worker.drain_once()
    assert report3.claimed == 0
    store.close()


def test_crash_after_side_effect_duplicates_delivery() -> None:
    """At-least-once proof: an ack lost after a successful delivery duplicates it."""
    store = SQLite(":memory:")
    todos = Stream(Todo, name="todos")
    side_effects: list[str] = []

    def handler(commit: Commit[Todo, BaseModel]) -> None:
        side_effects.append("side effect")

    app = _app(todos, handler)
    _seed(store, app)

    class AckLost:
        """A store whose settle always fails, like a crash before the ack."""

        def __init__(self, inner: SQLite) -> None:
            self._inner = inner
            self.capabilities = inner.capabilities

        def commit(self, requests):
            return self._inner.commit(requests)

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
            raise StoreError("ack lost")

    worker = Worker(app, AckLost(store), queue="q", lease=timedelta(milliseconds=50))
    with pytest.raises(StoreError):
        worker.drain_once()
    assert len(side_effects) == 1
    time.sleep(0.1)  # the lost lease expires
    report2 = Worker(app, store, queue="q").drain_once()
    assert report2.delivered == 1
    assert len(side_effects) == 2  # duplicate delivery is the at-least-once proof
    store.close()


def test_inline_and_durable_envelopes_identical() -> None:
    """004/F10: inline and worker Commit envelopes are field-for-field equal."""
    store = SQLite(":memory:")
    todos = Stream(Todo, name="todos")
    inline_seen: list[Commit[Todo, BaseModel]] = []
    durable_seen: list[Commit[Todo, BaseModel]] = []

    def inline_handler(commit: Commit[Todo, BaseModel]) -> None:
        inline_seen.append(commit)

    def outbox_handler(commit: Commit[Todo, BaseModel]) -> None:
        durable_seen.append(commit)

    app = App(
        id="demo",
        streams=[todos],
        subscriptions=[
            Subscription(id="inline", stream=todos, handler=inline_handler),
            Subscription(
                id="outbox",
                stream=todos,
                handler=outbox_handler,
                delivery=Outbox(queue="q"),
            ),
        ],
    )
    runtime = app.bind(store)
    first = runtime[todos].create(Todo(text="a"))
    changed = runtime[todos].change(first, done=True)
    runtime[todos].replace(changed, Todo(text="c", done=False))

    assert len(inline_seen) == 3
    worker = Worker(app, store, queue="q")
    worker.drain_once()
    worker.drain_once()
    worker.drain_once()

    assert len(durable_seen) == 3
    for inline, durable in zip(inline_seen, durable_seen, strict=True):
        assert inline.kind == durable.kind
        assert inline.revision.revision == durable.revision.revision
        assert inline.revision.committed_at == durable.revision.committed_at
        assert inline.changed == durable.changed
        assert inline.revision.digest == durable.revision.digest
        assert inline.revision.state.model_dump() == durable.revision.state.model_dump()
    store.close()


def test_replace_reports_changed_for_replaced_keys() -> None:
    """F3: replace diffs against the *previous* state, not the new one.

    ARCHITECTURE.md §2.2: an inline handler and a worker rebuilding the same
    commit from the log receive field-for-field identical envelopes. When
    ``replace`` passed the new state as the ``before`` document, the inline
    envelope always reported ``changed=frozenset()`` while the durable one
    reported the true diff.
    """
    store = SQLite(":memory:")
    todos = Stream(Todo, name="todos")
    inline_seen: list[Commit[Todo, BaseModel]] = []
    durable_seen: list[Commit[Todo, BaseModel]] = []

    def handler(commit: Commit[Todo, BaseModel]) -> None:
        inline_seen.append(commit)

    def outbox_handler(commit: Commit[Todo, BaseModel]) -> None:
        durable_seen.append(commit)

    app = App(
        id="demo",
        streams=[todos],
        subscriptions=[
            Subscription(id="inline", stream=todos, handler=handler),
            Subscription(
                id="outbox",
                stream=todos,
                handler=outbox_handler,
                delivery=Outbox(queue="q"),
            ),
        ],
    )
    runtime = app.bind(store)
    first = runtime[todos].create(Todo(text="a", done=False))
    replaced = runtime[todos].replace(first, Todo(text="b", done=True))

    assert inline_seen[1].changed == frozenset({"text", "done"})
    # the durable reconstruction agrees
    worker = Worker(app, store, queue="q")
    assert worker.drain_once().delivered == 2
    assert durable_seen[1].changed == frozenset({"text", "done"})
    assert inline_seen[1].changed == durable_seen[1].changed
    assert inline_seen[1].revision.digest == durable_seen[1].revision.digest
    assert replaced.digest == durable_seen[1].revision.digest
    store.close()


def test_batch_replace_reports_changed_for_replaced_keys() -> None:
    """F3 applies to the batch path too: ``BatchCollection.replace`` must diff
    against the previous state."""
    store = SQLite(":memory:")
    todos = Stream(Todo, name="todos")
    inline_seen: list[Commit[Todo, BaseModel]] = []

    def handler(commit: Commit[Todo, BaseModel]) -> None:
        inline_seen.append(commit)

    app = App(
        id="demo",
        streams=[todos],
        subscriptions=[Subscription(id="inline", stream=todos, handler=handler)],
    )
    runtime = app.bind(store)
    first = runtime[todos].create(Todo(text="a", done=False))
    with runtime.batch() as batch:
        batch[todos].replace(first, Todo(text="b", done=True))
    assert inline_seen[1].changed == frozenset({"text", "done"})
    store.close()


def test_one_function_two_subscriptions_two_deliveries() -> None:
    """004/F14: one function under two subscriptions produces two intents and
    two deliveries without violating the unique constraint."""
    store = SQLite(":memory:")
    todos = Stream(Todo, name="todos")
    deliveries: list[str] = []

    def handler(commit: Commit[Todo, BaseModel]) -> None:
        deliveries.append(commit.revision.revision_id.hex)

    app = App(
        id="demo",
        streams=[todos],
        subscriptions=[
            Subscription(
                id="sub.one", stream=todos, handler=handler, delivery=Outbox(queue="q")
            ),
            Subscription(
                id="sub.two", stream=todos, handler=handler, delivery=Outbox(queue="q")
            ),
        ],
    )
    _seed(store, app)
    worker = Worker(app, store, queue="q")
    report = worker.drain_once()
    assert report.delivered == 2
    assert deliveries[0] == deliveries[1]
    store.close()


def test_worker_report_structure() -> None:
    report = WorkerReport(claimed=3, delivered=2, retried=1, dead_lettered=0)
    assert report.claimed == 3
    assert report.delivered == 2
    assert report.retried == 1
    assert report.dead_lettered == 0


def test_dead_lettered_raises_when_expected() -> None:
    err = DeadLettered("boom")
    assert isinstance(err, Exception)


def test_run_forever_stops_via_stop_flag() -> None:
    """F11: run_forever exits promptly when stop() is called, even mid-sleep."""
    import threading

    store = SQLite(":memory:")
    todos = Stream(Todo, name="todos")
    app = _app(todos, lambda c: None)
    worker = Worker(app, store, queue="q")

    def stopper() -> None:
        time.sleep(0.05)
        worker.stop()

    thread = threading.Thread(target=stopper)
    thread.start()
    worker.run_forever(poll=timedelta(milliseconds=5))
    thread.join(timeout=5)
    assert not thread.is_alive()
    store.close()


def test_last_error_redacted_no_credentials() -> None:
    store = SQLite(":memory:")
    todos = Stream(Todo, name="todos")

    def leaks(commit: Commit[Todo, BaseModel]) -> None:
        raise RuntimeError("failed connecting to postgres://user:hunter2@db:5432/x")

    app = _app(todos, leaks, max_attempts=2)
    _seed(store, app)
    worker = Worker(app, store, queue="q")
    worker.drain_once()
    time.sleep(0.03)
    report = worker.drain_once()
    assert report.dead_lettered == 1
    from sqlalchemy import text

    with store.engine.connect() as conn:
        row = conn.execute(
            text("SELECT last_error FROM eventic_intent WHERE status = 'dead'")
        ).first()
    assert row is not None
    assert "hunter2" not in (row[0] or "")
    assert "***" in (row[0] or "")
    store.close()
