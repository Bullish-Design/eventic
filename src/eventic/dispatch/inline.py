"""Inline dispatch — strictly post-durability (I7, F3).

Runs ``after_commit`` interceptors (inner→outer) then matching ``inline``
subscriptions (MRO order, registration order within a class). Every failure is
isolated and logged — it must never propagate to the writer. Called only by
the ``UnitOfWork`` after the durability line.
"""

from __future__ import annotations

import logging

from ..event import Event
from ..subscribe import subscriptions_for

logger = logging.getLogger(__name__)


def dispatch_inline(event: Event) -> None:
    cls = type(event.record)
    cfg = cls.__eventic__

    for itc in reversed(cfg.interceptors):
        try:
            itc.after_commit(event)
        except Exception:
            logger.exception(
                "after_commit interceptor %s failed for %s(%s)",
                type(itc).__name__,
                cls.__name__,
                event.record.id,
            )

    for sub in subscriptions_for(cls):
        if sub.via != "inline" or not sub.matches(event.kind):
            continue
        try:
            sub.fn(event)
        except Exception:
            logger.exception(
                "subscription %s failed for %s(%s)",
                sub.handler_id,
                cls.__name__,
                event.record.id,
            )
