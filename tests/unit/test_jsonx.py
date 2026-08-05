"""Phase 1: jsonx — canonical bytes and digest."""

from __future__ import annotations

import hashlib

import pytest

from eventic.jsonx import JsonObject, JsonValue, canonical_bytes, digest


def test_canonical_bytes_order_independent() -> None:
    a: JsonObject = {"b": 1, "a": {"d": 2, "c": 3}}
    b: JsonObject = {"a": {"c": 3, "d": 2}, "b": 1}
    assert canonical_bytes(a) == canonical_bytes(b)


def test_canonical_bytes_minimal_separators() -> None:
    assert canonical_bytes({"a": 1, "b": [1, 2]}) == b'{"a":1,"b":[1,2]}'


def test_canonical_bytes_utf8_preserved() -> None:
    assert canonical_bytes({"s": "héllo ☃"}) == '{"s":"héllo ☃"}'.encode()


def test_canonical_bytes_scalars_and_null() -> None:
    for tree in [None, True, False, 0, 1, -1, 1.5, "x", [1, "a", None], {}]:
        assert isinstance(canonical_bytes(tree), bytes)


def test_canonical_bytes_rejects_nan_and_infinity() -> None:
    with pytest.raises(ValueError):
        canonical_bytes(float("nan"))
    with pytest.raises(ValueError):
        canonical_bytes(float("inf"))
    with pytest.raises(ValueError):
        canonical_bytes(float("-inf"))
    with pytest.raises(ValueError):
        canonical_bytes({"x": float("nan")})
    with pytest.raises(ValueError):
        canonical_bytes({"x": float("inf")})
    with pytest.raises(ValueError):
        canonical_bytes([float("-inf")])


def test_canonical_bytes_float_identity() -> None:
    assert canonical_bytes({"x": 1.5}) == b'{"x":1.5}'


def test_digest_matches_sha256() -> None:
    payload = b'{"a":1}'
    assert digest(payload) == hashlib.sha256(payload).hexdigest()


def test_digest_hex_64() -> None:
    assert len(digest(b"x")) == 64


def test_digest_distinguishes_content() -> None:
    assert digest(b'{"a":1}') != digest(b'{"a":2}')


def test_type_aliases_are_subscriptable() -> None:
    tree: JsonValue = {"k": ["v", 1, None]}
    assert tree == {"k": ["v", 1, None]}
