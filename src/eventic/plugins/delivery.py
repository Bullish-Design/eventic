"""Delivery seam — how an emitted event reaches its handlers, per named mode.

Default provider: ``SyncDelivery`` (``mode="sync"``, in-process, strictly
post-commit — the row is durable before any handler runs, I7). Handlers are
isolated: a failure is logged, never propagated to the writer. A durable
backend (DBOS outbox) is the opt-in ``eventic[dbos]`` extra.
"""

from __future__ import annotations

import logging

from ..events import Event, _HANDLERS
from . import Plugin, Seam

logger = logging.getLogger(__name__)


class SyncDelivery(Plugin):
    """Default backend: run matching sync handlers immediately, in MRO order."""

    seam = Seam.DELIVERY
    provides = {"delivery"}
    requires = set()
    mode = "sync"

    def deliver(self, event: Event) -> None:
        for c in type(event.record).__mro__:
            for kind, fn, mode, queue, h_id in _HANDLERS.get(c, []):
                if mode != "sync" or kind not in ("*", event.kind):
                    continue
                try:
                    fn(event)
                except Exception:
                    logger.exception(
                        "event handler %s failed for %s(%s)",
                        fn.__name__,
                        type(event.record).__name__,
                        event.record.id,
                    )
