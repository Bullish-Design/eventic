"""Engine-registry tests (Step 1): connect(), the NotConnected contract."""

import pytest

from eventic.connect import _reset, connect, engine
from eventic.errors import NotConnected


@pytest.fixture(autouse=True)
def _clean_engine():
    _reset()
    yield
    _reset()


def test_engine_before_connect_raises():
    with pytest.raises(NotConnected):
        engine()


def test_connect_creates_records_table(tmp_path):
    connect(f"sqlite:///{tmp_path / 'a.db'}")
    from sqlalchemy import inspect

    insp = inspect(engine())
    assert set(insp.get_table_names()) >= {"records"}


def test_reconnect_swaps_engine(tmp_path):
    connect(f"sqlite:///{tmp_path / 'a.db'}")
    first = engine()
    connect(f"sqlite:///{tmp_path / 'b.db'}")
    second = engine()
    assert first is not second
    assert str(second.url).endswith("b.db")


def test_create_tables_false_leaves_schema_untouched(tmp_path):
    connect(f"sqlite:///{tmp_path / 'a.db'}", create_tables=False)
    from sqlalchemy import inspect

    insp = inspect(engine())
    assert "records" not in insp.get_table_names()
