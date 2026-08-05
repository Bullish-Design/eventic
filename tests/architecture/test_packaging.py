"""Phase 0 gate: the built wheel ships the right artifacts and nothing else."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

WHEEL_GLOBS = ("dist/*.whl",)


def _wheel_path() -> Path:
    wheels = sorted(Path("dist").glob("*.whl"))
    if not wheels:
        pytest.skip("wheel not built; run `uv build` first")
    return wheels[-1]


def _wheel_names() -> set[str]:
    with zipfile.ZipFile(_wheel_path()) as zf:
        return {n for n in zf.namelist()}


def test_wheel_contains_py_typed() -> None:
    names = _wheel_names()
    assert any(n.endswith("eventic/py.typed") for n in names), names


def test_wheel_contains_no_scratch() -> None:
    names = _wheel_names()
    assert not any("/.scratch/" in n or n.startswith(".scratch") for n in names)


def test_wheel_contains_no_test_package() -> None:
    names = _wheel_names()
    bad = any(n.startswith("eventic/tests") or n.startswith("tests") for n in names)
    assert not bad


def test_wheel_contains_no_probes() -> None:
    names = _wheel_names()
    assert not any("/probes/" in n for n in names)


def test_sdist_excludes_scratch_uvlock() -> None:
    import tarfile

    sdist = sorted(Path("dist").glob("*.tar.gz"))
    if not sdist:
        pytest.skip("sdist not built; run `uv build` first")
    with tarfile.open(sdist[-1]) as tf:
        names = tf.getnames()
    assert not any("/.scratch" in n for n in names)
    assert not any("/probes" in n for n in names)
    assert not any(n.endswith("uv.lock") for n in names)
