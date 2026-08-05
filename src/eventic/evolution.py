"""Schema evolution: upcasters, chain validation, and the upcast function.

Upcasters receive a JSON tree and return a JSON tree. They are deterministic,
get no store, clock, or context, and are declared by the application.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol, runtime_checkable

from eventic.errors import IncompleteUpcasterChain
from eventic.jsonx import JsonObject


@runtime_checkable
class Upcaster(Protocol):
    """One deterministic JSON-to-JSON transition ``from_version -> to_version``."""

    from_version: int
    to_version: int

    def __call__(self, tree: JsonObject) -> JsonObject: ...


def validate_chain(
    upcasters: Mapping[int, Upcaster],
    *,
    from_version: int,
    to_version: int,
    subject: str,
) -> None:
    """Assert ``upcasters`` forms a complete ``from_version -> to_version`` chain.

    Raises :class:`IncompleteUpcasterChain` naming the first missing or
    disconnected transition.
    """
    version = from_version
    while version < to_version:
        upcaster = upcasters.get(version)
        if upcaster is None:
            raise IncompleteUpcasterChain(
                f"{subject}: missing upcaster {version} -> {version + 1}"
            )
        if upcaster.to_version != version + 1:
            raise IncompleteUpcasterChain(
                f"{subject}: upcaster {version} must target {version + 1}, "
                f"not {upcaster.to_version}"
            )
        version = upcaster.to_version


def upcast(
    tree: JsonObject,
    upcasters: Mapping[int, Upcaster],
    *,
    from_version: int,
    to_version: int,
) -> JsonObject:
    """Apply the declared upcaster chain, raising on any gap."""
    version = from_version
    while version < to_version:
        upcaster = upcasters.get(version)
        if upcaster is None:
            raise IncompleteUpcasterChain(
                f"missing upcaster {version} -> {version + 1} while upcasting"
            )
        tree = upcaster(tree)
        version = upcaster.to_version
    return tree


def make_upcaster(
    from_version: int, to_version: int, fn: Callable[[JsonObject], JsonObject]
) -> Upcaster:
    """Build an ``Upcaster`` from a plain function."""
    _from, _to = from_version, to_version

    class _Upcaster:
        from_version = _from
        to_version = _to

        def __call__(self, tree: JsonObject) -> JsonObject:
            return fn(tree)

    return _Upcaster()
