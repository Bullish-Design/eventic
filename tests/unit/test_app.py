"""Phase 4: App — collected declaration validation, no I/O, identity."""

from __future__ import annotations

import copy

import pytest
from pydantic import BaseModel

from eventic.app import App
from eventic.envelopes import Commit
from eventic.errors import CapabilityUnsupported, ConfigError
from eventic.meta import Meta, NoMeta
from eventic.stream import Stream
from eventic.subscription import Outbox, Subscription


class Todo(BaseModel):
    text: str
    done: bool = False


class RequestMeta(BaseModel):
    request_id: str


def handler(c: Commit[Todo, RequestMeta]) -> None:
    return None


def bad_arity(c: Commit[Todo, RequestMeta], extra: int) -> None:
    return None


async def async_handler(c: Commit[Todo, RequestMeta]) -> None:
    return None


def test_app_basic() -> None:
    todos = Stream(Todo, name="todos")
    app = App(
        id="demo",
        streams=[todos],
        meta=Meta(RequestMeta, version=1),
        subscriptions=[Subscription(id="sub.1", stream=todos, handler=handler)],
    )
    assert app.id == "demo"
    assert app.streams == (todos,)
    assert app.on_inline_error == "raise"


def test_app_defaults() -> None:
    todos = Stream(Todo, name="todos")
    app = App(id="demo", streams=[todos])
    assert app.meta is NoMeta
    assert app.subscriptions == ()


def test_collected_validation_reports_all() -> None:
    todos = Stream(Todo, name="todos")
    other = Stream(Todo, name="other")
    with pytest.raises(ConfigError) as excinfo:
        App(
            id="demo",
            streams=[todos, todos],
            subscriptions=[
                Subscription(id="s", stream=other, handler=async_handler),
                Subscription(id="s", stream=todos, handler=handler),
            ],
        )
    msg = str(excinfo.value)
    assert "duplicate stream name: todos" in msg
    assert "duplicate subscription id: s" in msg
    assert "stream other is not installed" in msg
    assert "async handlers are not supported in 1.0" in msg


def test_handler_arity_reported() -> None:
    todos = Stream(Todo, name="todos")
    with pytest.raises(ConfigError) as excinfo:
        App(
            id="demo",
            streams=[todos],
            subscriptions=[Subscription(id="s", stream=todos, handler=bad_arity)],
        )
    assert "exactly one positional argument" in str(excinfo.value)


def test_handler_wrong_annotation_reported() -> None:
    todos = Stream(Todo, name="todos")

    def wrong(c: int) -> None:
        return None

    with pytest.raises(ConfigError) as excinfo:
        App(
            id="demo",
            streams=[todos],
            subscriptions=[Subscription(id="s", stream=todos, handler=wrong)],
        )
    assert "must be typed as Commit" in str(excinfo.value)


def test_construction_performs_no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("I/O during App construction")

    monkeypatch.setattr("socket.socket", boom)
    monkeypatch.setattr("builtins.open", boom)
    todos = Stream(Todo, name="todos")
    App(
        id="demo",
        streams=[todos],
        subscriptions=[Subscription(id="s", stream=todos, handler=handler)],
    )


def test_app_identity() -> None:
    todos = Stream(Todo, name="todos")
    a = App(id="demo", streams=[todos])
    b = App(id="demo", streams=[todos])
    c = App(id="other", streams=[todos])
    assert a == b
    assert a != c
    assert hash(a) == hash(b)
    assert len({a, b, c}) == 2


def test_app_deep_copy() -> None:
    todos = Stream(Todo, name="todos")
    app = App(id="demo", streams=[todos])
    clone = copy.deepcopy(app)
    assert clone == app
    assert clone.id == app.id


def test_bind_rejects_outbox_without_capability() -> None:
    class NoOutboxStore:
        @property
        def capabilities(self) -> object:
            return type("Caps", (), {"outbox": False})()

    todos = Stream(Todo, name="todos")
    app = App(
        id="demo",
        streams=[todos],
        subscriptions=[
            Subscription(id="s", stream=todos, handler=handler, delivery=Outbox())
        ],
    )
    with pytest.raises(CapabilityUnsupported):
        app.bind(NoOutboxStore())  # type: ignore[arg-type]
