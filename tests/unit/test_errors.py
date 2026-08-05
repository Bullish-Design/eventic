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
