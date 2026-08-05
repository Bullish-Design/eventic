"""Shared test infrastructure.

- ``store``: a fresh, scoped ``Store`` per test — the only sanctioned way to
  bind one (there are no ``_reset_*`` hooks anymore, I8).
- ``_no_leaked_active_store``: if a test dies mid-way and leaves the active
  store bound, unbind it so the next test starts clean.
- ``_collect_garbage``: surfaces leaked sqlite connections' ResourceWarnings
  at the test that leaked them (the ``-W error`` gate).
"""

import gc

import pytest


@pytest.fixture(autouse=True)
def _collect_garbage():
    yield
    gc.collect()


@pytest.fixture(autouse=True)
def _no_leaked_active_store():
    yield
    from eventic.store import _ACTIVE

    s = _ACTIVE.get()
    if s is not None:
        try:
            s.deactivate()
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _dispose_stores():
    """Every Store created during a test gets its engine disposed at the end,
    so the ``-W error`` gate never trips on pooled SQLite connections."""
    from eventic.store import Store as _Store

    created: list = []
    original = _Store.__init__

    def __init__(self, *a, **k):
        original(self, *a, **k)
        created.append(self)

    _Store.__init__ = __init__
    yield
    _Store.__init__ = original
    for s in created:
        try:
            s.engine.dispose()
        except Exception:
            pass


@pytest.fixture()
def store(tmp_path):
    """A fresh, scoped Store on a throwaway SQLite file."""
    from eventic import Store

    s = Store(f"sqlite:///{tmp_path / 't.db'}", create_tables=True)
    with s:
        yield s
    s.engine.dispose()  # release pooled connections (the -W error gate)
