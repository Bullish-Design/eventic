"""I6 — the core import graph must never touch dbos/fastapi (R-P3).

Phase-1 form: the *new* core modules (everything except ``eventic/dbos/``)
must contain no dbos/fastapi import. The live ``import eventic`` assertion is
added at Step 9, when ``eventic/__init__.py`` is rewritten off the old surface
(which still imports DBOS until the Phase-6 swap); the Step-13 success matrix
runs the final check.
"""

import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[2] / "eventic"

# the new core module graph (the old modules are the target of Step 12)
CORE_FILES = [
    "connect.py",
    "errors.py",
    "events.py",
    "models.py",
    "pipeline.py",
    "record.py",
    "plugins/__init__.py",
    "plugins/codec.py",
    "plugins/delivery.py",
    "plugins/identity.py",
    "plugins/interceptor.py",
    "plugins/persistence.py",
]


@pytest.mark.parametrize("rel", CORE_FILES)
def test_core_source_has_no_dbos_or_fastapi_import(rel):
    text = (SRC / rel).read_text()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import dbos", "from dbos", "import fastapi", "from fastapi")):
            raise AssertionError(f"{rel}:{line}")
        if "dbos" in stripped and "dbos/" not in stripped and "eventic.dbos" not in stripped:
            # allow comments referencing the optional package, but no imports
            assert not stripped.startswith(("import", "from")), f"{rel}:{line}"


def test_dbos_extra_is_the_only_newcore_dbos_importer():
    """Within the new core module graph, only eventic/dbos/ imports dbos.
    (The old 0.1 modules still do — they are deleted at Step 12.)"""
    for rel in CORE_FILES:
        text = (SRC / rel).read_text()
        for line in text.splitlines():
            if line.strip().startswith(("import dbos", "from dbos")):
                raise AssertionError(f"{rel}:{line}")
