"""Phase 14: the clean-wheel smoke test — the documented production path from
an installed artifact with no project checkout."""

from __future__ import annotations

import shutil
import subprocess
import venv
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent.parent


def _wheel() -> Path:
    wheels = sorted((ROOT / "dist").glob("*.whl"))
    assert wheels, "run `uv build` first"
    return wheels[-1]


def _install_and_run(wheel: Path, tmp: Path) -> subprocess.CompletedProcess[str]:
    env_path = tmp / "venv"
    venv.EnvBuilder(with_pip=True).create(env_path)
    python = env_path / "bin" / "python"
    subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", str(wheel)],
        check=True,
        capture_output=True,
    )
    script = """
import sqlite3
from pydantic import BaseModel
from eventic import App, Stream
from eventic.sql import SQLite

class Todo(BaseModel):
    text: str
    done: bool = False

todos = Stream(Todo, name="todos")
ev = App(id="demo", streams=[todos]).bind(SQLite(":memory:"))
t = ev[todos].create(Todo(text="a"))
t = ev[todos].change(t, done=True)
assert ev[todos].get(t.id).digest == t.digest
assert [r.revision for r in ev[todos].history(t.id).items] == [0, 1]
print("WHEEL-SMOKE-OK")
"""
    return subprocess.run([str(python), "-c", script], capture_output=True, text=True)


@pytest.mark.slow
def test_clean_wheel_smoke(tmp_path: Path) -> None:
    wheel = _wheel()
    result = _install_and_run(wheel, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "WHEEL-SMOKE-OK" in result.stdout


def test_wheel_contains_cli_migrations_testing() -> None:
    with zipfile.ZipFile(_wheel()) as zf:
        names = zf.namelist()
    joined = "\n".join(names)
    assert "eventic/cli/main.py" in joined
    assert "eventic/sql/migrations/versions/0001_baseline.py" in joined
    assert "eventic/testing/conformance/store.py" in joined
    assert "eventic/py.typed" in joined


def test_minimal_install_imports_without_postgres() -> None:
    """pip install eventic (no extras) imports and runs SQLite."""
    tmp = Path("/tmp") / "minimal-install-check"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir()
    try:
        result = _install_and_run(_wheel(), tmp)
        assert result.returncode == 0, result.stderr
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
