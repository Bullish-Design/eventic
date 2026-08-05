"""Shared test infrastructure for the new suites (core/plugins/dbos).

After each test, collect garbage so leaked sqlite connections surface their
ResourceWarning at the test that leaked them (the Step-13 `pytest -W error`
gate) instead of at a random later GC.
"""

import gc

import pytest


@pytest.fixture(autouse=True)
def _collect_garbage():
    yield
    gc.collect()
