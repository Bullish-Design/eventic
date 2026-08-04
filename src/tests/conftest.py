"""Shared test harness: one fresh Eventic (SQLite) per test.

DBOS 2.x defaults the *system* database to the same sqlite file as the app
database when the app database URL is sqlite, so this fixture needs no
Postgres at all. ``Eventic.reset()`` (Step 5.3) tears the singleton down so
the next test re-inits cleanly.
"""

import pytest

from eventic import Eventic


@pytest.fixture()
def eventic(tmp_path):
    """One fresh Eventic per test: SQLite app DB, SQLite system DB (DBOS 2.x default)."""
    db_url = f"sqlite:///{tmp_path / 'eventic.db'}"
    Eventic.init(name="eventic-test", database_url=db_url)
    Eventic.launch()
    yield Eventic.instance()
    Eventic.destroy()
    Eventic.reset()  # clears singleton + _store so the next test re-inits
