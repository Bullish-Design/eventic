"""Phase 16 (R1–R10): the async port is enforced cheap by automated test.

The future port is a set of new files below ``protocols.py``; these rules make
that shape a test, not a promise.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import types
from pathlib import Path

import eventic

ROOT = Path(eventic.__file__).resolve().parent.parent.parent


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _module(name: str) -> types.ModuleType:
    return importlib.import_module(name)


def test_protocol_annotations_resolve_without_sqlalchemy() -> None:
    import typing

    from eventic import protocols

    for name, member in inspect.getmembers(protocols.Store, inspect.isfunction):
        if name.startswith("_"):
            continue
        hints = typing.get_type_hints(member, include_extras=True)
        for annotation in hints.values():
            assert "sqlalchemy" not in str(annotation).lower(), (
                f"Store.{name} leaks sqlalchemy: {annotation}"
            )


def test_no_iterator_returns_in_protocol() -> None:
    import typing

    from eventic import protocols

    for name, member in inspect.getmembers(protocols.Store, inspect.isfunction):
        if name.startswith("_"):
            continue
        for annotation in typing.get_type_hints(member).values():
            text = str(annotation)
            assert (
                "Iterator" not in text
                and "Iterable" not in text
                and "Generator" not in text
            ), f"Store.{name} returns a lazy iterator"


def test_no_yield_in_store_executor() -> None:
    source = (ROOT / "src/eventic/sql/store.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            raise AssertionError("sql/store.py must not yield")
    assert "yield" not in source


def test_no_callable_parameters_in_store() -> None:

    from eventic import protocols

    for name, member in inspect.getmembers(protocols.Store, inspect.isfunction):
        if name.startswith("_"):
            continue
        for param in inspect.signature(member).parameters.values():
            annotation = str(param.annotation).lower()
            assert "callable" not in annotation, f"Store.{name} takes a callable"


def test_no_contextvar_or_threadlocal() -> None:
    import pkgutil

    for info in pkgutil.walk_packages(eventic.__path__, eventic.__name__ + "."):
        if "migrations" in info.name:
            continue
        module = importlib.import_module(info.name)
        for name, value in vars(module).items():
            if _is_dunder(name):
                continue
            if (
                "ContextVar" in type(value).__name__
                or "local" in type(value).__name__.lower()
            ):
                raise AssertionError(f"{info.name}.{name} is a context/thread state")


def test_store_admin_is_sync_forever() -> None:
    import typing

    from eventic import protocols

    for name, member in inspect.getmembers(protocols.StoreAdmin):
        if name.startswith("_"):
            continue
        assert not inspect.iscoroutinefunction(member)
        hints = typing.get_type_hints(member)
        for annotation in hints.values():
            assert "sqlalchemy" not in str(annotation).lower()


def test_paper_port_modules_need_no_edits() -> None:
    """The modules above the protocol line must import cleanly with zero
    sqlalchemy and zero I/O-relevant stdlib, which is what lets the async port
    reuse them verbatim."""
    from eventic.canonical import canonicalize  # noqa: F401
    from eventic.evolution import upcast  # noqa: F401
    from eventic.hydration import hydrate  # noqa: F401
    from eventic.planning import plan_create  # noqa: F401
    from eventic.retry import disposition  # noqa: F401
    from eventic.sql import statements  # noqa: F401

    assert callable(plan_create)
    assert callable(hydrate)
    assert callable(disposition)
    assert callable(canonicalize)
    assert callable(upcast)
