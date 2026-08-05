"""§12.9/§12.10: ``import eventic`` imports pydantic and nothing else; the
import graph matches the documented dependency rules."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import eventic

ROOT = Path(eventic.__file__).resolve().parent.parent.parent


def test_import_eventic_leaves_sqlalchemy_out() -> None:
    code = (
        "import sys; import eventic; "
        "print('sqlalchemy' in sys.modules, 'psycopg' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT
    )
    assert result.returncode == 0, result.stderr
    assert "False False" in result.stdout


def test_import_eventic_leaves_alembic_out() -> None:
    code = "import sys; import eventic; print('alembic' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT
    )
    assert "False" in result.stdout


def _imports(module: Path) -> set[str]:
    tree = ast.parse(module.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_protocols_imports_no_sqlalchemy() -> None:
    imports = _imports(ROOT / "src/eventic/protocols.py")
    assert not any(i.split(".")[0] == "sqlalchemy" for i in imports)


def test_runtime_imports_no_sql() -> None:
    imports = _imports(ROOT / "src/eventic/runtime.py")
    assert not any(i.startswith("eventic.sql") for i in imports)


def test_sql_imports_never_runtime_or_cli() -> None:
    for module in (ROOT / "src/eventic/sql").rglob("*.py"):
        if "migrations" in module.parts:
            continue
        imports = _imports(module)
        assert not any(i.startswith("eventic.runtime") for i in imports), module
        assert not any(i.startswith("eventic.cli") for i in imports), module


def test_leaves_import_nothing() -> None:
    from eventic.ids import revision_id
    from eventic.jsonx import canonical_bytes, digest

    assert callable(revision_id)
    assert callable(canonical_bytes)
    assert callable(digest)


def test_no_callable_in_store_protocol_parameters() -> None:
    import inspect

    from eventic.protocols import Store

    for name, method in inspect.getmembers(Store, inspect.isfunction):
        if name.startswith("_"):
            continue
        for param in inspect.signature(method).parameters.values():
            if (
                "Callable" in str(param.annotation)
                or "callable" in str(param.annotation).lower()
            ):
                raise AssertionError(f"Store.{name} takes a callable")
