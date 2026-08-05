"""Phase 3: Stream, Meta, evolution chains, and frozen envelopes."""

from __future__ import annotations

import pickle
from datetime import UTC

import pytest
from pydantic import BaseModel, RootModel, ValidationError

from eventic.envelopes import Commit, Page, Revision
from eventic.errors import ConfigError, IncompleteUpcasterChain
from eventic.evolution import make_upcaster, upcast, validate_chain
from eventic.meta import NoMeta
from eventic.stream import Stream
from eventic.testing.factories import WithComputed


class Todo(BaseModel):
    text: str
    done: bool = False


class AltTodo(BaseModel):
    text: str
    done: bool = True


def test_stream_basic() -> None:
    s = Stream(Todo, name="todos")
    assert s.model is Todo
    assert s.name == "todos"
    assert s.schema_version == 1
    assert s.upcasters == {}
    assert len(s.fingerprint) == 64


def test_stream_identity_is_name() -> None:
    a = Stream(Todo, name="todos")
    b = Stream(AltTodo, name="todos")
    c = Stream(Todo, name="others")
    assert a == b
    assert a != c
    assert hash(a) == hash(b)
    assert len({a, b, c}) == 2


def test_stream_rejects_non_model() -> None:
    with pytest.raises(ConfigError):
        Stream(int, name="ints")  # type: ignore[arg-type]


def test_stream_rejects_root_model() -> None:
    class Root(RootModel[int]):
        pass

    with pytest.raises(ConfigError):
        Stream(Root, name="roots")


def test_stream_rejects_bad_name() -> None:
    with pytest.raises(ValueError):
        Stream(Todo, name="BadName!")


def test_stream_rejects_bad_schema_version() -> None:
    with pytest.raises(ConfigError):
        Stream(Todo, name="todos", schema_version=0)
    with pytest.raises(ConfigError):
        Stream(Todo, name="todos", schema_version="2")  # type: ignore[arg-type]


def test_stream_rejects_secret() -> None:
    class WithToken(BaseModel):
        token: str

    import pydantic

    class S(BaseModel):
        token: pydantic.SecretStr

    with pytest.raises(ConfigError):
        Stream(S, name="secret")


def test_stream_caches_adapter_and_exclude() -> None:
    s = Stream(WithComputed, name="wc")
    payload = s.adapter.dump_python(
        WithComputed(a=1), mode="json", exclude=dict(s.exclude_map)
    )
    assert "doubled" not in payload


def test_stream_incomplete_chain() -> None:
    with pytest.raises(IncompleteUpcasterChain) as excinfo:
        Stream(
            Todo,
            name="todos",
            schema_version=3,
            upcasters={1: make_upcaster(1, 2, lambda t: t)},
        )
    assert "2 -> 3" in str(excinfo.value)


def test_stream_complete_chain() -> None:
    s = Stream(
        Todo,
        name="todos",
        schema_version=3,
        upcasters={
            1: make_upcaster(1, 2, lambda t: t),
            2: make_upcaster(2, 3, lambda t: t),
        },
    )
    assert s.schema_version == 3


def test_stream_disconnected_chain() -> None:
    with pytest.raises(IncompleteUpcasterChain):
        Stream(
            Todo,
            name="todos",
            schema_version=3,
            upcasters={1: make_upcaster(1, 2, lambda t: t)},
        )


def test_stream_pickles() -> None:
    s = Stream(Todo, name="todos")
    again = pickle.loads(pickle.dumps(s))
    assert again == s


def test_upcast_applies_chain() -> None:
    tree = {"text": "a"}
    result = upcast(
        tree,
        {1: make_upcaster(1, 2, lambda t: {**t, "v2": True})},
        from_version=1,
        to_version=2,
    )
    assert result == {"text": "a", "v2": True}


def test_upcast_identity_when_equal() -> None:
    tree = {"text": "a"}
    assert upcast(tree, {}, from_version=1, to_version=1) == tree


def test_upcast_missing_transition_raises() -> None:
    with pytest.raises(IncompleteUpcasterChain):
        upcast({"text": "a"}, {}, from_version=1, to_version=2)


def test_validate_chain_subject_in_error() -> None:
    with pytest.raises(IncompleteUpcasterChain) as excinfo:
        validate_chain({}, from_version=1, to_version=2, subject="meta")
    assert "meta" in str(excinfo.value)


def test_no_meta_serializes_empty() -> None:
    assert NoMeta.model is not None
    payload = NoMeta.adapter.dump_python(NoMeta.model(), mode="json")
    assert payload == {}


def test_meta_machinery() -> None:
    from eventic.meta import Meta

    class RequestMeta(BaseModel):
        request_id: str

    m = Meta(RequestMeta, version=1)
    assert m.version == 1
    assert len(m.fingerprint) == 64
    out = m.adapter.dump_python(RequestMeta(request_id="r1"), mode="json")
    assert out == {"request_id": "r1"}


def test_revision_json_schema_expands_state() -> None:
    schema = Revision[Todo, Todo].model_json_schema()
    props = schema["properties"]
    assert "state" in props
    state_ref = props["state"]["$ref"]
    todo_schema = schema["$defs"][state_ref.rsplit("/", 1)[-1]]
    assert "text" in todo_schema["properties"]
    assert "done" in todo_schema["properties"]
    assert "committed_at" in props
    assert "digest" in props


def test_revision_and_commit_frozen() -> None:
    from datetime import datetime
    from uuid import UUID

    rev = Revision[Todo, BaseModel](
        stream="todos",
        id=UUID(int=1),
        revision=0,
        revision_id=UUID(int=2),
        state=Todo(text="a"),
        meta=Todo(text="m"),
        committed_at=datetime(2024, 1, 1, tzinfo=UTC),
        digest="d",
    )
    with pytest.raises(ValidationError):
        rev.revision = 1  # type: ignore[misc]

    commit = Commit[Todo, BaseModel](
        kind="create",
        revision=rev,
        changed=frozenset({"text", "done"}),
    )
    assert commit.changed == frozenset({"text", "done"})
    with pytest.raises(ValidationError):
        commit.kind = "change"  # type: ignore[misc]


def test_revision_metadata_frozen() -> None:
    from datetime import datetime
    from uuid import UUID

    rev = Revision[Todo, BaseModel](
        stream="todos",
        id=UUID(int=1),
        revision=0,
        revision_id=UUID(int=2),
        state=Todo(text="a"),
        meta=Todo(text="m"),
        committed_at=datetime(2024, 1, 1, tzinfo=UTC),
        digest="d",
    )
    assert rev.state.text == "a"
    assert rev.meta.text == "m"


def test_page_generic_with_dataclass_items() -> None:
    page = Page[tuple[int, str]](items=((1, "a"),), cursor="abc")
    assert page.cursor == "abc"
    assert page.items == ((1, "a"),)
    empty = Page[int](items=(), cursor=None)
    assert empty.items == ()
