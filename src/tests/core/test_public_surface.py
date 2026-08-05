"""Public-surface tests (Step 9): the new package exports and the I6 live check."""

import subprocess
import sys


def test_public_surface():
    from eventic import (  # noqa: F401
        DiffStorage,
        EventicError,
        MissingCapability,
        NotConnected,
        Plugin,
        PluginConflictError,
        Record,
        Seam,
        StaleVersionError,
        connect,
        on_commit,
        use,
    )

    assert hasattr(Record, "save")
    assert hasattr(Record, "update")
    assert hasattr(Record, "edit")


def test_import_eventic_is_dbos_free_live():
    """I6 final form (Step-13 matrix): a fresh interpreter importing the
    package root must not pull dbos or fastapi into sys.modules."""
    code = (
        "import sys, eventic; "
        "assert 'dbos' not in sys.modules, 'dbos leaked'; "
        "assert 'fastapi' not in sys.modules, 'fastapi leaked'; "
        "print('DBOS-FREE')"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "DBOS-FREE" in out.stdout


def test_dbos_extra_is_explicit_import():
    """The adapter is never auto-imported by the package root (D17)."""
    code = (
        "import sys, eventic; "
        "assert 'eventic.dbos' not in sys.modules; "
        "print('NO-AUTO-DBOS')"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "NO-AUTO-DBOS" in out.stdout
