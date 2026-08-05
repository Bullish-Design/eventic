"""Phase 1: ids — deterministic identity, stable across processes and platforms."""

from __future__ import annotations

import pickle
import uuid

import pytest
from pydantic import TypeAdapter, ValidationError

from eventic.ids import NS, AggregateKey, StreamName, revision_id

_validate_name = TypeAdapter(StreamName)


def _via(name: object) -> str:
    return _validate_name.validate_python(name)


def test_revision_id_stable_vectors() -> None:
    aid = uuid.UUID("11111111-2222-3333-4444-555555555555")
    assert str(revision_id("todos", aid, 0)) == "5c33763f-afde-5b0a-9e18-519df4ffba6a"
    assert str(revision_id("todos", aid, 1)) == "fdd47226-0dab-5446-85a3-56f030f2a154"
    assert str(revision_id("audits", aid, 0)) != str(revision_id("todos", aid, 0))


def test_revision_id_expected_form() -> None:
    aid = uuid.UUID("11111111-2222-3333-4444-555555555555")
    expected = uuid.uuid5(NS, "todos:11111111-2222-3333-4444-555555555555:2")
    assert revision_id("todos", aid, 2) == expected


def test_stream_name_valid_shapes() -> None:
    for name in ["a", "todos", "todo.list", "todo_list", "todos-v1", "0a9_.-z"]:
        assert _via(name) == name


@pytest.mark.parametrize(
    "name",
    ["", "Todo", "TODO", "todos!", "to dos", "a" * 65, "-x", ".x"],
)
def test_stream_name_invalid_shapes(name: str) -> None:
    with pytest.raises(ValidationError):
        _via(name)


def test_stream_name_rejects_non_string() -> None:
    with pytest.raises(ValidationError):
        _via(123)


def test_aggregate_key_hashable_and_equal() -> None:
    aid = uuid.uuid4()
    a = AggregateKey("todos", aid)
    b = AggregateKey("todos", aid)
    c = AggregateKey("audits", aid)
    assert a == b
    assert a != c
    assert hash(a) == hash(b)
    assert len({a, b, c}) == 2
    assert pickle.loads(pickle.dumps(a)) == a


def test_aggregate_key_validates_stream() -> None:
    with pytest.raises(ValueError):
        AggregateKey("Bad!", uuid.uuid4())


def test_aggregate_key_fields() -> None:
    aid = uuid.uuid4()
    key = AggregateKey("todos", aid)
    assert key.stream == "todos"
    assert key.aggregate_id == aid
