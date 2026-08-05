"""
Opt-in queue decorator.

Semantics (no more "run now AND later" — that was at-least-twice execution):
* A method marked ``@evented`` is NOT run inline. It is scheduled on the
  per-class queue and executes as a DBOS workflow on a serialized *snapshot*
  of self.
* For aggregate mutations, prefer passing ``self.id`` and re-hydrating inside
  the step, so the queued run observes fresh state.
"""

from functools import wraps
from typing import Any, Callable, Optional

from dbos import DBOS


def evented(fn: Optional[Callable] = None):
    """Explicit opt-in: schedule this method on the class queue.

    Usable both as ``@evented`` and ``@evented()``.
    """
    if fn is None:  # @evented() with parens
        return lambda f: _mark(f)

    # bare decorator inside the class body: mark for the metaclass
    return _mark(fn)


def _mark(fn: Callable) -> Callable:
    """Stamp the method; RecordMeta discovers the mark at class-creation time."""
    fn.__eventic_evented__ = True  # type: ignore[attr-defined]
    return fn


def _queue_method(fn: Callable):
    """Metaclass hook: register fn as a DBOS step and return a scheduling wrapper.

    Registration happens at class-creation time so ``get_func_info`` succeeds
    when the queue worker executes the function (C2), and the per-class Queue
    (declared exactly once by RecordMeta, M7) is reused for every method.
    """
    step = DBOS.step()(fn)  # registers fn -> get_func_info succeeds (C2)

    @wraps(fn)
    def inner(self: Any, *args: Any, **kwargs: Any):
        return self.__class__.queue.enqueue(step, self, *args, **kwargs)

    return inner
