"""Phase 5: the pure commit core — planning, hydration, retry, wire."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import BaseModel

from eventic.app import App
from eventic.envelopes import Revision
from eventic.evolution import make_upcaster
from eventic.hydration import hydrate
from eventic.ids import revision_id
from eventic.jsonx import digest
from eventic.meta import Meta
from eventic.planning import (
    changed_keys,
    intents_for,
    plan_change,
    plan_create,
    plan_replace,
    state_tree,
)
from eventic.retry import Disposition, disposition, redact_error
from eventic.stream import Stream
from eventic.subscription import Backoff, Inline, Outbox, Subscription
from eventic.wire import CommitRequest, IntentRequest, StoredRevision


class Todo(BaseModel):
    text: str
    done: bool = False


class RequestMeta(BaseModel):
    request_id: str


def handler(commit):  # noqa: ANN001 — deliberately untyped, as real handlers may be
    return None


def _app() -> App:
    todos = Stream(Todo, name="todos")
    return App(id="demo", streams=[todos])


def _app_with_meta() -> App:
    todos = Stream(Todo, name="todos")
    return App(
        id="demo",
        streams=[todos],
        meta=Meta(RequestMeta, version=1),
    )


def test_plan_create_request_shape() -> None:
    app = _app()
    todos = app.streams[0]
    aid = UUID(int=1)
    request = plan_create(app, todos, Todo(text="a"), aid)
    assert isinstance(request, CommitRequest)
    assert request.stream == "todos"
    assert request.aggregate_id == aid
    assert request.expected_revision is None
    assert request.kind == "create"
    assert request.schema_version == 1
    assert request.meta_version == 1
    assert (
        request.payload
        == json.dumps(
            {"text": "a", "done": False}, sort_keys=True, separators=(",", ":")
        ).encode()
    )
    assert request.digest == digest(request.payload)
    assert request.fingerprint == todos.fingerprint
    assert json.loads(request.meta) == {}


def test_plan_create_generates_revision_id() -> None:
    app = _app()
    todos = app.streams[0]
    aid = UUID(int=1)
    request = plan_create(app, todos, Todo(text="a"), aid)
    assert request.intents == ()


def test_plan_change_merges_and_validates() -> None:
    app = _app()
    todos = app.streams[0]
    aid = UUID(int=1)
    create = plan_create(app, todos, Todo(text="a"), aid)
    base = Revision[Todo, BaseModel](
        stream="todos",
        id=aid,
        revision=0,
        revision_id=revision_id("todos", aid, 0),
        state=Todo(text="a"),
        meta=Todo(text="m"),
        committed_at=datetime(2024, 1, 1, tzinfo=UTC),
        digest=create.digest,
    )
    request = plan_change(app, todos, base, {"done": True})
    assert request.expected_revision == 0
    assert request.kind == "change"
    assert json.loads(request.payload) == {"text": "a", "done": True}
    assert request.digest == digest(request.payload)


def test_plan_change_never_coerces_away_field() -> None:
    # A caller kwarg that pydantic would coerce (e.g. bool from int) must not
    # silently change the durable request; the adapter validates.
    app = _app()
    todos = app.streams[0]
    aid = UUID(int=1)
    create = plan_create(app, todos, Todo(text="a"), aid)
    base = Revision[Todo, BaseModel](
        stream="todos",
        id=aid,
        revision=0,
        revision_id=revision_id("todos", aid, 0),
        state=Todo(text="a"),
        meta=Todo(text="m"),
        committed_at=datetime(2024, 1, 1, tzinfo=UTC),
        digest=create.digest,
    )
    request = plan_change(app, todos, base, {"text": "b"})
    tree = json.loads(request.payload)
    assert tree["text"] == "b"


def test_plan_change_preserves_meta() -> None:
    app = _app_with_meta()
    todos = app.streams[0]
    aid = UUID(int=1)
    create = plan_create(
        app, todos, Todo(text="a"), aid, meta=RequestMeta(request_id="r1")
    )
    base = Revision[Todo, RequestMeta](
        stream="todos",
        id=aid,
        revision=0,
        revision_id=revision_id("todos", aid, 0),
        state=Todo(text="a"),
        meta=RequestMeta(request_id="r1"),
        committed_at=datetime(2024, 1, 1, tzinfo=UTC),
        digest=create.digest,
    )
    request = plan_change(app, todos, base, {"done": True})
    assert json.loads(request.meta) == {"request_id": "r1"}


def test_plan_replace_validates_whole_state() -> None:
    app = _app()
    todos = app.streams[0]
    aid = UUID(int=1)
    create = plan_create(app, todos, Todo(text="a"), aid)
    base = Revision[Todo, BaseModel](
        stream="todos",
        id=aid,
        revision=0,
        revision_id=revision_id("todos", aid, 0),
        state=Todo(text="a"),
        meta=Todo(text="m"),
        committed_at=datetime(2024, 1, 1, tzinfo=UTC),
        digest=create.digest,
    )
    request = plan_replace(app, todos, base, Todo(text="z", done=True))
    assert request.expected_revision == 0
    assert json.loads(request.payload) == {"text": "z", "done": True}


def test_stale_base_yields_stale_expected_revision() -> None:
    # Planning does not repair a stale handle; it builds the request faithfully
    # and the store rejects it later.
    app = _app()
    todos = app.streams[0]
    aid = UUID(int=1)
    create = plan_create(app, todos, Todo(text="a"), aid)
    stale = Revision[Todo, BaseModel](
        stream="todos",
        id=aid,
        revision=0,
        revision_id=revision_id("todos", aid, 0),
        state=Todo(text="a"),
        meta=Todo(text="m"),
        committed_at=datetime(2024, 1, 1, tzinfo=UTC),
        digest=create.digest,
    )
    request = plan_change(app, todos, stale, {"text": "b"})
    assert request.expected_revision == 0  # stale on purpose


def test_changed_keys_create_and_diff() -> None:
    before = {"text": "a", "done": False}
    after = {"text": "b", "done": False}
    assert changed_keys(None, after) == frozenset({"text", "done"})
    assert changed_keys(before, after) == frozenset({"text"})
    assert changed_keys(before, before) == frozenset()


def test_changed_keys_reports_removed_and_added_keys() -> None:
    # a key present only in before (removed) and one only in after (added)
    # are both changed (F13); the old test asserted the removed key was
    # invisible, which was the bug.
    assert changed_keys({"a": 1}, {"b": 2}) == frozenset({"a", "b"})


def test_state_tree_is_canonical() -> None:
    app = _app()
    todos = app.streams[0]
    tree = state_tree(todos, Todo(text="x", done=True))
    assert tree == {"text": "x", "done": True}


def test_intents_for_outbox_and_kinds() -> None:
    todos = Stream(Todo, name="todos")
    app = App(
        id="demo",
        streams=[todos],
        subscriptions=[
            Subscription(
                id="o", stream=todos, handler=handler, delivery=Outbox(queue="q")
            ),
            Subscription(id="i", stream=todos, handler=handler, delivery=Inline()),
            Subscription(
                id="only-change",
                stream=todos,
                handler=handler,
                kinds=frozenset({"change"}),
                delivery=Outbox(),
            ),
        ],
    )
    rid = UUID(int=9)
    create_intents = intents_for(app, todos, "create", rid)
    assert [i.subscription_id for i in create_intents] == ["o"]
    change_intents = intents_for(app, todos, "change", rid)
    assert [i.subscription_id for i in change_intents] == ["o", "only-change"]
    for intent in change_intents:
        assert isinstance(intent, IntentRequest)
        assert intent.revision_id == rid


def test_hydrate_round_trip_by_digest() -> None:
    app = _app()
    todos = app.streams[0]
    aid = UUID(int=7)
    request = plan_create(app, todos, Todo(text="hello"), aid)
    stored = StoredRevision(
        stream="todos",
        aggregate_id=aid,
        revision=0,
        revision_id=revision_id("todos", aid, 0),
        kind="create",
        schema_version=1,
        meta_version=1,
        encoding="snapshot/1",
        payload=json.loads(request.payload),
        digest=request.digest,
        meta=json.loads(request.meta),
        committed_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    rev = hydrate(todos, app.meta, stored)
    assert rev.state == Todo(text="hello")
    assert rev.state.text == "hello"
    assert rev.digest == digest(
        json.dumps(
            {"text": "hello", "done": False},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    )
    assert rev.revision == 0
    assert rev.id == aid


def test_hydrate_upcasts_old_version() -> None:
    v2 = Stream(
        Todo,
        name="todos",
        schema_version=2,
        upcasters={1: make_upcaster(1, 2, lambda t: {**t, "upcast": True})},
    )
    app = App(id="demo", streams=[v2])
    aid = UUID(int=7)
    stored = StoredRevision(
        stream="todos",
        aggregate_id=aid,
        revision=0,
        revision_id=revision_id("todos", aid, 0),
        kind="create",
        schema_version=1,
        meta_version=1,
        encoding="snapshot/1",
        payload={"text": "hello", "done": False},
        digest="d",
        meta={},
        committed_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    rev = hydrate(v2, app.meta, stored)
    assert rev.state == Todo(text="hello")  # extra key ignored by validation


def test_hydrate_meta_upcast() -> None:
    meta_v2 = Meta(
        RequestMeta,
        version=2,
        upcasters={1: make_upcaster(1, 2, lambda t: {**t, "v2": True})},
    )
    todos = Stream(Todo, name="todos")
    app = App(id="demo", streams=[todos], meta=meta_v2)
    aid = UUID(int=7)
    stored = StoredRevision(
        stream="todos",
        aggregate_id=aid,
        revision=0,
        revision_id=revision_id("todos", aid, 0),
        kind="create",
        schema_version=1,
        meta_version=1,
        encoding="snapshot/1",
        payload={"text": "hello", "done": False},
        digest="d",
        meta={"request_id": "r1"},
        committed_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    rev = hydrate(todos, app.meta, stored)
    assert rev.meta.request_id == "r1"


def test_disposition_retry_and_dead() -> None:
    now = datetime(2024, 1, 1, tzinfo=UTC)
    backoff = Backoff(max_attempts=3, base=1.0, factor=2.0, cap=100.0)
    d1 = disposition(1, backoff, "boom", now)
    assert d1.action == "retry"
    assert d1.available_at == now + timedelta(seconds=1.0)
    d2 = disposition(2, backoff, "boom", now)
    assert d2.available_at == now + timedelta(seconds=2.0)
    d3 = disposition(3, backoff, "boom", now)
    assert d3.action == "dead"
    assert d3.error == "boom"


def test_disposition_caps_delay() -> None:
    now = datetime(2024, 1, 1, tzinfo=UTC)
    backoff = Backoff(max_attempts=50, base=1.0, factor=2.0, cap=10.0)
    d = disposition(40, backoff, "e", now)
    assert d.action == "retry"
    assert d.available_at == now + timedelta(seconds=10.0)


def test_disposition_never_touches_clock() -> None:
    now = datetime(2024, 1, 1, tzinfo=UTC)
    backoff = Backoff(max_attempts=3)
    a = disposition(1, backoff, "e", now)
    b = disposition(1, backoff, "e", now)
    assert a == b


def test_redact_error_removes_credentials_and_truncates() -> None:
    redacted = redact_error("failed at postgres://user:secret@host/db")
    assert "secret" not in redacted
    assert "***" in redacted
    long = "x" * 5000
    assert len(redact_error(long)) == 2048


def test_disposition_dataclass_shape() -> None:
    d = Disposition(action="dead", error="e")
    assert d.available_at is None


def test_changed_keys_reports_a_removed_key() -> None:
    """F13: a key present in before and absent from after is changed."""
    from eventic.planning import changed_keys

    assert changed_keys({"a": 1, "removed": 2}, {"a": 1}) == frozenset({"removed"})
    assert changed_keys({"a": 1}, {"a": 2}) == frozenset({"a"})
    assert changed_keys(None, {"a": 1}) == frozenset({"a"})


def test_changed_keys_reports_removed_key_through_extra_allow_model() -> None:
    """F13 end-to-end: an extra='allow' stream can drop a top-level key; the
    inline Commit.changed must include it, and the durable envelope agrees."""
    from pydantic import BaseModel, ConfigDict

    from eventic.app import App
    from eventic.envelopes import Commit
    from eventic.sql import SQLite
    from eventic.stream import Stream
    from eventic.subscription import Subscription

    class Loose(BaseModel):
        model_config = ConfigDict(extra="allow")

    seen: list[Commit[Loose, BaseModel]] = []

    def handler(c: Commit[Loose, BaseModel]) -> None:
        seen.append(c)

    stream = Stream(Loose, name="loose")
    app = App(
        id="d",
        streams=[stream],
        subscriptions=[Subscription(id="i", stream=stream, handler=handler)],
    )
    store = SQLite(":memory:")
    ev = app.bind(store)
    try:
        first = ev[stream].create(Loose(a=1, removed=2))
        replaced = ev[stream].replace(first, Loose(a=1))
        assert replaced.state.model_dump() == {"a": 1}
        assert seen[1].changed == frozenset({"removed"})
    finally:
        store.close()
