"""
Queue decorator that pushes method-calls onto a DBOS queue
named per-class (passed in at decoration time).
"""

from functools import wraps
from typing import Any, Callable, TypeVar

from dbos import Queue

# _Fn = TypeVar("_Fn", bound=Callable[..., Any])


def evented(queue_name: str):
    """Wrap a public method to synchronously execute **and** enqueue itself on
    a per‑class DBOS :class:`Queue` for asynchronous/parallel processing."""

    q = Queue(queue_name, concurrency=1)

    def decorator(fn):
        @wraps(fn)
        def inner(self: "Record", *args, **kwargs):
            result = fn(self, *args, **kwargs)  # run synchronously first
            # enqueue the same callable + args for background processing
            q.enqueue(fn, self, *args, **kwargs)
            return result

        return inner

    return decorator
