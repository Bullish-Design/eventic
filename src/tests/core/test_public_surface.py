"""Public surface + I6 (the core import graph never touches dbos/fastapi)."""

import pathlib
import subprocess
import sys


def test_public_surface():
    from eventic import (  # noqa: F401
        ConfigError,
        Delta,
        Draft,
        EventicError,
        HandlerCollision,
        Interceptor,
        NotConnected,
        OutboxDispatcher,
        Record,
        RecordNotFound,
        SeamMismatch,
        Snapshot,
        StaleVersionError,
        Store,
        StreamCollision,
        UsageError,
        Veto,
        active_store,
        connect,
        on_commit,
        version_id,
    )

    for name in ("save", "update", "draft", "get", "history", "where"):
        assert hasattr(Record, name)


def test_dead_0_2_surface_is_gone():
    """F8/F12: use(), Plugin, Seam, plugins.* no longer exist."""
    import pytest

    with pytest.raises(ImportError):
        from eventic import use  # noqa: F401
    with pytest.raises(ImportError):
        from eventic import Plugin, Seam  # noqa: F401
    with pytest.raises(ImportError):
        import eventic.plugins  # noqa: F401


def test_import_eventic_is_dbos_free_live():
    """I6 live check: a fresh interpreter importing the package root must not
    pull dbos or fastapi into sys.modules."""
    code = (
        "import sys, eventic; "
        "assert 'dbos' not in sys.modules, 'dbos leaked'; "
        "assert 'fastapi' not in sys.modules, 'fastapi leaked'; "
        "print('DBOS-FREE')"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "DBOS-FREE" in out.stdout


def test_contrib_is_explicit_import():
    """The DBOS driver is never auto-imported by the package root."""
    code = (
        "import sys, eventic; "
        "assert 'eventic.contrib' not in sys.modules; "
        "print('NO-AUTO-DBOS')"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "NO-AUTO-DBOS" in out.stdout


def test_only_contrib_imports_dbos():
    """Source scan: no dbos/fastapi import in the core or the examples
    (the DBOS driver is the only importer)."""
    root = pathlib.Path(__file__).resolve().parents[2] / "eventic"
    for py in root.rglob("*.py"):
        if "contrib" in py.parts or "examples" in py.parts:
            continue
        for line in py.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith(("import dbos", "from dbos", "import fastapi", "from fastapi")):
                raise AssertionError(f"{py.relative_to(root)}:{line}")
