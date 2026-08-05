"""Phase 1: the full exception tree, structured attributes, safe rendering."""

from __future__ import annotations

import uuid

import pytest

from eventic.errors import (
    CapabilityUnsupported,
    ConfigError,
    DeadLettered,
    DeliveryError,
    DuplicateId,
    EncodingError,
    EventicError,
    IncompleteUpcasterChain,
    InlineDispatchError,
    NotFound,
    RevisionConflict,
    StoreError,
    UndecodableRevision,
    UnknownStream,
    UnsupportedHandler,
    UsageError,
)

_ALL = [
    EventicError,
    ConfigError,
    DuplicateId,
    UnknownStream,
    UnsupportedHandler,
    IncompleteUpcasterChain,
    UsageError,
    NotFound,
    RevisionConflict,
    EncodingError,
    UndecodableRevision,
    CapabilityUnsupported,
    StoreError,
    DeliveryError,
    InlineDispatchError,
    DeadLettered,
]


def test_hierarchy() -> None:
    assert issubclass(ConfigError, EventicError)
    assert issubclass(DuplicateId, ConfigError)
    assert issubclass(UnknownStream, ConfigError)
    assert issubclass(UnsupportedHandler, ConfigError)
    assert issubclass(IncompleteUpcasterChain, ConfigError)
    assert issubclass(UsageError, EventicError)
    assert issubclass(NotFound, EventicError)
    assert issubclass(RevisionConflict, EventicError)
    assert issubclass(EncodingError, EventicError)
    assert issubclass(UndecodableRevision, EventicError)
    assert issubclass(CapabilityUnsupported, EventicError)
    assert issubclass(StoreError, EventicError)
    assert issubclass(DeliveryError, EventicError)
    assert issubclass(InlineDispatchError, DeliveryError)
    assert issubclass(DeadLettered, DeliveryError)


def test_not_found_is_not_key_error() -> None:
    assert not issubclass(NotFound, KeyError)


def test_structured_attributes() -> None:
    aid = uuid.uuid4()
    err = RevisionConflict(
        "stale base",
        stream="todos",
        aggregate_id=aid,
        revision=3,
        subscription_id="sub.1",
        extra="x",
    )
    assert err.stream == "todos"
    assert err.aggregate_id == aid
    assert err.revision == 3
    assert err.subscription_id == "sub.1"
    assert err.attrs == {"extra": "x"}


def test_defaults_are_none() -> None:
    err = DuplicateId("duplicate")
    assert err.stream is None
    assert err.aggregate_id is None
    assert err.revision is None
    assert err.subscription_id is None
    assert err.attrs == {}


def test_renders_without_payload() -> None:
    for cls in _ALL:
        msg = f"test message for {cls.__name__}"
        assert str(cls(msg)) == msg


@pytest.mark.parametrize("cls", _ALL)
def test_all_constructible(cls: type[EventicError]) -> None:
    e = cls("boom")
    assert isinstance(e, EventicError)
    assert isinstance(e, Exception)


# ---------------------------------------------------------------------------
# Behaviour: each declaration fault raises the class §2.1 assigns to it.
# (F6: the classes used to exist and subclass ConfigError but were raised
# nowhere — every fault surfaced as a bare ConfigError.)
# ---------------------------------------------------------------------------

from pydantic import BaseModel  # noqa: E402

from eventic.app import App  # noqa: E402
from eventic.envelopes import Commit  # noqa: E402
from eventic.stream import Stream  # noqa: E402
from eventic.subscription import Subscription  # noqa: E402


class _Todo(BaseModel):
    text: str = ""


def _sync(c: Commit[_Todo, BaseModel]) -> None: ...


async def _async(c: Commit[_Todo, BaseModel]) -> None: ...


def _bad_arity(c: Commit[_Todo, BaseModel], extra: int) -> None: ...


def test_duplicate_id_raised_for_duplicate_stream_name() -> None:
    todos = Stream(_Todo, name="todos")
    with pytest.raises(DuplicateId):
        App(id="a", streams=[todos, todos])


def test_duplicate_id_raised_for_duplicate_subscription_id() -> None:
    todos = Stream(_Todo, name="todos")
    with pytest.raises(DuplicateId):
        App(
            id="a",
            streams=[todos],
            subscriptions=[
                Subscription(id="s", stream=todos, handler=_sync),
                Subscription(id="s", stream=todos, handler=_sync),
            ],
        )


def test_unknown_stream_raised_for_uninstalled_stream() -> None:
    todos = Stream(_Todo, name="todos")
    elsewhere = Stream(_Todo, name="elsewhere")
    with pytest.raises(UnknownStream):
        App(
            id="a",
            streams=[todos],
            subscriptions=[Subscription(id="s", stream=elsewhere, handler=_sync)],
        )


def test_unsupported_handler_raised_for_async() -> None:
    todos = Stream(_Todo, name="todos")
    with pytest.raises(UnsupportedHandler):
        App(
            id="a",
            streams=[todos],
            subscriptions=[Subscription(id="s", stream=todos, handler=_async)],
        )


def test_unsupported_handler_raised_for_bad_arity() -> None:
    todos = Stream(_Todo, name="todos")
    with pytest.raises(UnsupportedHandler):
        App(
            id="a",
            streams=[todos],
            subscriptions=[Subscription(id="s", stream=todos, handler=_bad_arity)],
        )


def test_mixed_faults_raise_the_common_base_with_all_messages() -> None:
    """Several distinct fault classes together raise ConfigError, listing every
    failure — the §2.1 'reported together' requirement."""
    todos = Stream(_Todo, name="todos")
    other = Stream(_Todo, name="other")
    with pytest.raises(ConfigError) as excinfo:
        App(
            id="a",
            streams=[todos, todos],
            subscriptions=[
                Subscription(id="s", stream=other, handler=_async),
            ],
        )
    assert not isinstance(excinfo.value, DuplicateId)
    msg = str(excinfo.value)
    assert "duplicate stream name: todos" in msg
    assert "stream other is not installed" in msg
    assert "async handlers are not supported" in msg


def test_single_fault_class_carries_all_its_messages() -> None:
    """Two DuplicateId faults raise DuplicateId with both messages joined."""
    todos = Stream(_Todo, name="todos")
    with pytest.raises(DuplicateId) as excinfo:
        App(
            id="a",
            streams=[todos, todos],
            subscriptions=[
                Subscription(id="s", stream=todos, handler=_sync),
                Subscription(id="s", stream=todos, handler=_sync),
            ],
        )
    msg = str(excinfo.value)
    assert "duplicate stream name: todos" in msg
    assert "duplicate subscription id: s" in msg


# ---------------------------------------------------------------------------
# F9: Meta equality includes version (App equality is identity-of-declaration)
# ---------------------------------------------------------------------------

from eventic.evolution import make_upcaster  # noqa: E402
from eventic.meta import Meta  # noqa: E402


def test_meta_equality_includes_version() -> None:
    m1 = Meta(_Todo, version=1)
    m2 = Meta(_Todo, version=2, upcasters={1: make_upcaster(1, 2, lambda t: t)})
    assert m1 != m2
    assert hash(m1) != hash(m2)


def test_meta_equality_same_version_still_equal() -> None:
    assert Meta(_Todo, version=1) == Meta(_Todo, version=1)
    assert hash(Meta(_Todo, version=1)) == hash(Meta(_Todo, version=1))


def test_app_equality_includes_meta_version() -> None:
    m1 = Meta(_Todo, version=1)
    m2 = Meta(_Todo, version=2, upcasters={1: make_upcaster(1, 2, lambda t: t)})
    a = App(id="a", streams=[Stream(_Todo, name="todos")], meta=m1)
    b = App(id="a", streams=[Stream(_Todo, name="todos")], meta=m2)
    assert a != b
    assert hash(a) != hash(b)
