"""Shared fixtures: a disposable SQLite store per test."""

from __future__ import annotations

from pathlib import Path

import pytest

from eventic.sql.store import SQLite


@pytest.fixture()
def sqlite_store(tmp_path: Path) -> SQLite:
    store = SQLite(str(tmp_path / "store.db"))
    yield store
    store.close()
