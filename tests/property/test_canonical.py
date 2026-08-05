"""Phase 2: canonicalization property over the type zoo, plus the safety nets."""

from __future__ import annotations

from itertools import count

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, TypeAdapter, computed_field

from eventic.canonical import build_exclude_map, canonicalize, contains_secret, verify
from eventic.errors import UndecodableRevision
from eventic.testing.factories import (
    ZOO,
    DeepComputed,
    WithComputed,
    WithNestedSecret,
    WithSecret,
)

_ALL_ZOO = [(member, instance) for member in ZOO for instance in member.instances]


@given(st.data())
@settings(max_examples=1000, deadline=None)
def test_zoo_property(data: st.DataObject) -> None:
    member, instance = data.draw(st.sampled_from(_ALL_ZOO))
    adapter = TypeAdapter(member.model)
    exclude = build_exclude_map(member.model)
    first = canonicalize(adapter, exclude, instance)
    reparsed = adapter.validate_json(first)
    second = canonicalize(adapter, exclude, reparsed)
    assert second == first


def test_computed_fields_absent_at_every_depth() -> None:
    adapter = TypeAdapter(DeepComputed)
    exclude = build_exclude_map(DeepComputed)
    instance = DeepComputed.model_validate(
        {
            "tag": "t",
            "nested": {
                "name": "n",
                "child": {"a": 1},
                "children": [{"a": 2}],
                "mapping": {"k": {"a": 3}},
            },
            "depth_two": [],
            "depth_mapping": {},
        }
    )
    tree = adapter.dump_python(instance, mode="json")
    assert "upper" in tree
    assert "total" in tree["nested"]
    assert "doubled" in tree["nested"]["child"]
    payload = canonicalize(adapter, exclude, instance)
    assert b"doubled" not in payload
    assert b"total" not in payload
    assert b"upper" not in payload


def test_computed_inside_sequences_and_mappings_stripped() -> None:
    exclude = build_exclude_map(DeepComputed)
    assert exclude["nested"]["total"] is True
    assert exclude["nested"]["child"]["doubled"] is True
    assert exclude["nested"]["children"] == {"__all__": {"doubled": True}}
    assert exclude["nested"]["mapping"] == {"__all__": {"doubled": True}}


class _Nondeterministic(BaseModel):
    value: int

    @computed_field
    @property
    def now(self) -> int:
        return next(_counter)


_counter = count()


def test_verify_raises_on_walker_gap() -> None:
    """A gap in the static walk becomes a loud write-time error.

    Simulate a walker gap (an empty exclude map) on a model whose computed
    field is nondeterministic: the first dump and the re-dump differ, so
    ``verify`` raises instead of committing an unstable payload.
    """
    adapter = TypeAdapter(_Nondeterministic)
    payload = canonicalize(adapter, {}, _Nondeterministic(value=1))
    with pytest.raises(UndecodableRevision):
        verify(adapter, {}, payload)


def test_verify_reports_pointer() -> None:
    adapter = TypeAdapter(_Nondeterministic)
    payload = canonicalize(adapter, {}, _Nondeterministic(value=1))
    with pytest.raises(UndecodableRevision) as excinfo:
        verify(adapter, {}, payload)
    assert excinfo.value.pointer is not None


def test_verify_passes_for_stable_model() -> None:
    adapter = TypeAdapter(WithComputed)
    exclude = build_exclude_map(WithComputed)
    payload = canonicalize(adapter, exclude, WithComputed(a=2))
    verify(adapter, exclude, payload)  # does not raise


def test_field_reordering_does_not_change_bytes() -> None:
    class First(BaseModel):
        alpha: int
        beta: str

    class Second(BaseModel):
        beta: str
        alpha: int

    a = canonicalize(TypeAdapter(First), {}, First(alpha=1, beta="x"))
    b = canonicalize(TypeAdapter(Second), {}, Second(beta="x", alpha=1))
    assert a == b


def test_secret_rejected_by_contains_secret() -> None:
    assert contains_secret(WithSecret)
    assert contains_secret(WithNestedSecret)


class NoSecret(BaseModel):
    value: str


def test_no_secret_false() -> None:
    assert not contains_secret(NoSecret)


def test_secret_never_masked_into_canonical() -> None:
    # The trap: SecretStr dumps as "**********"; the round trip then passes
    # while the data is destroyed. Reject at declaration instead.
    adapter = TypeAdapter(WithSecret)
    payload = canonicalize(adapter, {}, WithSecret(token="hunter2"))
    assert b"hunter2" not in payload
    assert b"**********" in payload
    roundtrip = adapter.validate_json(payload)
    assert roundtrip.token.get_secret_value() == "**********"


def test_canonical_bytes_differ_when_model_changes() -> None:
    a = canonicalize(TypeAdapter(WithComputed), {}, WithComputed(a=1))
    b = canonicalize(TypeAdapter(WithComputed), {}, WithComputed(a=2))
    assert a != b


def test_build_exclude_map_empty_for_plain_model() -> None:
    assert build_exclude_map(NoSecret) == {}
