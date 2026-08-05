"""Canonicalization: computed-field strip, canonical bytes, round-trip verify.

The two entry points used by the write path are :func:`canonicalize` (produce
the canonical bytes of a value) and :func:`verify` (prove those bytes decode to
an identical canonical form). ``verify`` is the real guarantee: any gap in the
static computed-field walk becomes a loud write-time error instead of an
undecodable row discovered months later.
"""

from __future__ import annotations

import json
import types
from collections.abc import Mapping
from typing import Annotated, Any, TypeVar, Union, get_args, get_origin

from pydantic import BaseModel, SecretStr, TypeAdapter

from eventic.errors import UndecodableRevision
from eventic.jsonx import JsonValue, canonical_bytes, digest

T = TypeVar("T")


def build_exclude_map(model: type[BaseModel]) -> dict[str, Any]:
    """Walk the annotated model graph for computed fields, at every depth.

    Returns a nested ``exclude`` specification in Pydantic's form, using
    ``__all__`` for sequences and mappings. Recursion is handled with a
    seen-set so self-referential models terminate.
    """
    return _walk_model(model, frozenset())


def contains_secret(model: type[BaseModel]) -> bool:
    """True if any field anywhere in the model graph is a ``SecretStr``.

    ``SecretStr`` serializes as ``"**********"`` in JSON mode, so persisting a
    model that contains one would destroy the value in an append-only log.
    ``Stream`` construction rejects such models.
    """
    if _SECRET_TYPE in (getattr(model, "__annotations__", {}) or {}).values():
        return True
    return _walk_secret(model, frozenset())


_SECRET_TYPE = SecretStr


def model_fingerprint(model: type[BaseModel]) -> str:
    """sha256 of the model's JSON schema, key-sorted.

    The fingerprint is stored per ``(stream, schema_version)`` and compared at
    ``eventic schema check`` time to catch a model change without a version
    bump.
    """
    schema = model.model_json_schema()
    return digest(canonical_bytes(schema))


def _walk_secret(model: type[BaseModel], seen: frozenset[type[BaseModel]]) -> bool:
    if model in seen:
        return False
    seen = seen | {model}
    for field in model.model_fields.values():
        if _annotation_has_secret(field.annotation, seen):
            return True
    return False


def _annotation_has_secret(annotation: Any, seen: frozenset[type[BaseModel]]) -> bool:
    if annotation is _SECRET_TYPE:
        return True
    origin = get_origin(annotation)
    if origin is types.UnionType or origin is Union:
        return any(_annotation_has_secret(arg, seen) for arg in get_args(annotation))
    if origin is Annotated:
        return _annotation_has_secret(get_args(annotation)[0], seen)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _walk_secret(annotation, seen)
    if origin in (list, set, tuple, dict):
        return any(_annotation_has_secret(arg, seen) for arg in get_args(annotation))
    return False


def _walk_model(
    model: type[BaseModel], seen: frozenset[type[BaseModel]]
) -> dict[str, Any]:
    if model in seen:
        return {}
    seen = seen | {model}
    exclude: dict[str, Any] = {}
    for name in model.model_computed_fields:
        exclude[name] = True
    for name, field in model.model_fields.items():
        nested = _walk_annotation(field.annotation, seen)
        if nested:
            exclude[name] = nested
    return exclude


def _walk_annotation(
    annotation: Any, seen: frozenset[type[BaseModel]]
) -> dict[str, Any] | None:
    origin = get_origin(annotation)
    if origin is types.UnionType or origin is Union:
        inner: dict[str, Any] | None = None
        for arg in get_args(annotation):
            inner = _walk_annotation(arg, seen)
            if inner:
                return inner
        return inner
    if origin is Annotated:
        return _walk_annotation(get_args(annotation)[0], seen)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        sub = _walk_model(annotation, seen)
        return sub or None
    if origin in (list, set, tuple):
        args = get_args(annotation)
        inner = _walk_annotation(args[0], seen) if args else None
        return {"__all__": inner} if inner else None
    if origin is dict:
        args = get_args(annotation)
        inner = _walk_annotation(args[1], seen) if args else None
        return {"__all__": inner} if inner else None
    return None


def canonicalize[T](
    adapter: TypeAdapter[T], exclude_map: Mapping[str, Any], value: T
) -> bytes:
    """Canonical bytes for a value: strip computed fields, dump JSON, sort keys."""
    tree = adapter.dump_python(
        value, mode="json", exclude=dict(exclude_map), by_alias=False
    )
    return canonical_bytes(tree)


def verify(
    adapter: TypeAdapter[Any], exclude_map: Mapping[str, Any], payload: bytes
) -> None:
    """Round-trip proof: re-validate, re-canonicalize, require byte equality."""
    value = adapter.validate_json(payload)
    again = canonicalize(adapter, exclude_map, value)
    if again == payload:
        return
    a: JsonValue = json.loads(payload)
    b: JsonValue = json.loads(again)
    pointer = _first_divergence(a, b)
    raise UndecodableRevision(
        "canonical document does not round-trip",
        pointer=pointer,
    )


def _first_divergence(a: JsonValue, b: JsonValue) -> str:
    """First JSON pointer (RFC 6901-ish) where two trees differ."""
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            if key in a and key in b:
                if a[key] != b[key]:
                    sub = _first_divergence(a[key], b[key])
                    return f"/{key}" + sub
            else:
                return f"/{key}"
        return ""
    if isinstance(a, list) and isinstance(b, list):
        for idx, (av, bv) in enumerate(zip(a, b, strict=False)):
            if av != bv:
                return f"/{idx}" + _first_divergence(av, bv)
        return f"/{len(a)}" if len(a) != len(b) else ""
    if a == b:
        return ""
    return "/<scalar>"
