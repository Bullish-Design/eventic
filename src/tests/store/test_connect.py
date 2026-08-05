"""Store/connect semantics (Step 3): explicit, context-bound; F23 defaults;
NotConnected; scoping; thread isolation.
"""

import threading

import pytest

from eventic import Store, connect
from eventic.errors import NotConnected


def test_active_store_before_binding_raises():
    with pytest.raises(NotConnected):
        from eventic import active_store

        active_store()


def test_store_does_not_create_tables_by_default(tmp_path):
    from sqlalchemy import inspect

    s = Store(f"sqlite:///{tmp_path / 'a.db'}")
    assert "eventic_log" not in inspect(s.engine).get_table_names()
    s.activate()
    assert "eventic_log" not in inspect(s.engine).get_table_names()  # still no DDL
    s.deactivate()


def test_connect_creates_tables(tmp_path):
    """F23: connect() is the dev sugar and does DDL; Store() does not."""
    from sqlalchemy import inspect

    c = connect(f"sqlite:///{tmp_path / 'b.db'}")
    names = set(inspect(c.engine).get_table_names())
    assert {"eventic_log", "eventic_head", "eventic_outbox"} <= names
    c.deactivate()


def test_with_store_scoping(tmp_path):
    from eventic import active_store

    with Store(f"sqlite:///{tmp_path / 'c.db'}", create_tables=True) as s:
        assert active_store() is s
    with pytest.raises(NotConnected):
        active_store()


def test_two_stores_coexist(tmp_path):
    from eventic import active_store

    a = Store(f"sqlite:///{tmp_path / 'a.db'}", create_tables=True)
    b = Store(f"sqlite:///{tmp_path / 'b.db'}", create_tables=True)
    with a:
        assert active_store() is a
        with b:
            assert active_store() is b
        assert active_store() is a
    with pytest.raises(NotConnected):
        active_store()


def test_concurrent_threads_see_their_own_binding(tmp_path):
    """ContextVars don't propagate to new threads — and that's the point: no
    shared mutable engine (I8). Each thread binds its own store."""
    from eventic import active_store

    url = f"sqlite:///{tmp_path / 't.db'}"
    with Store(url, create_tables=True) as main:
        assert active_store() is main
        results: list[bool] = []

        def worker():
            with Store(url, create_tables=False):
                results.append(active_store() is not main)

        ts = [threading.Thread(target=worker) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    assert results == [True] * 4
