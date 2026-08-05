"""Lifecycle regression tests (H3): explicit re-init/reset, no silent no-ops."""

import pytest

from eventic import Eventic


def test_second_init_raises(eventic):
    """H3: a second init()/create_app() must raise, not silently no-op."""
    with pytest.raises(RuntimeError):
        Eventic.init(name="eventic-test", database_url="sqlite:///other.db")


def test_reset_allows_reinit(eventic, tmp_path):
    """H3: reset() tears down the singleton so init() can run again."""
    Eventic.reset()
    Eventic.init(
        name="eventic-test", database_url=f"sqlite:///{tmp_path / 'reinit.db'}"
    )
    assert Eventic.instance() is not None


def test_create_app_returns_wired_app(eventic, tmp_path):
    """H3: create_app() returns a FastAPI app wired to a fresh Eventic."""
    Eventic.reset()
    app = Eventic.create_app("eventic-app", db_url=f"sqlite:///{tmp_path / 'app.db'}")
    assert app is not None
    assert Eventic.instance() is not None
