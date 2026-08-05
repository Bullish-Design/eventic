"""Regression tests for the P0 dispatcher findings (C2, C3, M7).

These reproduce the failures documented in REVIEW.md — expected to fail on
the pre-refactor code and pass from Step 3 onward.

Class names are unique per module (not ``Story``) so they do not collide
with ``test_record.py``'s classes in DBOS's global queue registry (DBOS
2.29 raises ``Queue has already been declared`` on re-declaration).

Classes with @evented methods are module-level (not defined inside test
functions) because DBOS's pickle-based serializer cannot pickle local
classes.
"""

from eventic import Record
from eventic.queues.dispatcher import evented


class QueuedStory(Record):
    title: str | None = None

    def describe(self) -> str:
        """Plain public method — must run inline, must not raise."""
        return f"QueuedStory({self.title})"

    @staticmethod
    def ping(value: int) -> int:
        """C3: staticmethods must not be wrapped/destroyed by the metaclass."""
        return value * 2


class EventedStory(Record):
    title: str | None = None
    ran: bool = False

    @evented
    def touch(self):
        self.ran = True
        return "done"


class MultiEventedStory(Record):
    title: str | None = None

    @evented
    def a(self):
        return 1

    @evented
    def b(self):
        return 2


def test_public_method_does_not_raise(eventic):
    """C2: a plain public method runs inline and never raises."""
    s = QueuedStory(title="x")
    assert s.describe() == "QueuedStory(x)"


def test_staticmethod_untouched(eventic):
    """C3: staticmethods survive class creation and work as-is."""
    assert QueuedStory.ping(21) == 42


def test_evented_schedules_without_inline_run(eventic):
    """C2: @evented methods are scheduled, never run synchronously (no double execution)."""
    s = EventedStory(title="x")
    assert s.ran is False

    handle = s.touch()
    assert handle is not None
    assert s.ran is False  # NOT executed inline
    assert handle.get_result() == "done"  # executed in the background

    fresh = EventedStory.hydrate(s.id)
    assert fresh.ran is True  # queued run persisted a new version


def test_no_duplicate_queue_declarations(eventic):
    """M7: several @evented methods on one class declare the Queue exactly once.

    DBOS 2.29 raises "Queue has already been declared" on a second declaration;
    reaching this point proves the metaclass's single cls.queue was reused.
    """
    assert MultiEventedStory.queue.name == "queue_multi_evented_story"
