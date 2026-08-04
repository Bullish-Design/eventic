"""Regression tests for the P0 dispatcher findings (C2, C3).

These reproduce the failures documented in REVIEW.md — expected to fail on
the pre-refactor code and pass from Step 3 onward.

Note: the class is named ``QueuedStory`` (not ``Story``) so it does not
collide with ``test_record.py``'s class in DBOS's global queue registry
(DBOS 2.29 raises ``Queue has already been declared`` on re-declaration).
"""

import pytest

from eventic import Record


class QueuedStory(Record):
    title: str | None = None

    def describe(self) -> str:
        """Plain public method — must run inline, must not raise."""
        return f"QueuedStory({self.title})"

    @staticmethod
    def ping(value: int) -> int:
        """C3: staticmethods must not be wrapped/destroyed by the metaclass."""
        return value * 2


def test_public_method_does_not_raise(eventic):
    """C2: a plain public method runs inline and never raises."""
    s = QueuedStory(title="x")
    assert s.describe() == "QueuedStory(x)"


def test_staticmethod_untouched(eventic):
    """C3: staticmethods survive class creation and work as-is."""
    assert QueuedStory.ping(21) == 42
