"""Run basedpyright over the typing fixtures and require zero diagnostics."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def _basedpyright() -> str:
    binary = shutil.which("basedpyright")
    if binary is None:
        raise RuntimeError("basedpyright not on PATH")
    return binary


def test_typing_fixtures_pass_basedpyright() -> None:
    files = [str(p) for p in sorted(FIXTURES.glob("*.py"))]
    assert files, "no typing fixtures found"
    result = subprocess.run(
        [_basedpyright(), *files],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
