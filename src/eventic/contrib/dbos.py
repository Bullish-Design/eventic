"""DBOS driver (``pip install eventic[dbos]`` only) — opt-in, explicit import.

DBOS is a *driver*, not the mechanism (CONCEPT §9): ``DbosStore`` joins the
enclosing DBOS workflow's transaction (so the durability line holds on the DBOS
path, F3), and ``DbosDispatcher`` drains the outbox onto DBOS queues — each
handler runs as a DBOS step, so DBOS owns retries and recovery. Deleted with
0.2: ``create_app`` (an app factory is not a persistence library's job) and
the module-level ``register_delivery`` side effect that flipped delivery on
process-wide (F10).
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from dbos import DBOS, Queue

from ..config import stream_class
from ..dispatch.outbox import claim_ready
from ..event import Event
from ..store import Store, active_store
from ..subscribe import _HANDLERS

logger = logging.getLogger(__name__)

__all__ = ["DbosStore", "DbosDispatcher", "queue"]

# explicit, memoized queue handles (DBOS rejects declaring the same name twice)
_QUEUES: dict[str, Queue] = {}


def queue(name: str, *, concurrency: int | None = None) -> Queue:
    """Create/reuse an explicit DBOS queue handle."""
    if name not in _QUEUES:
        _QUEUES[name] = Queue(name, concurrency=concurrency)
    return _QUEUES[name]


class DbosStore(Store):
    """A Store whose writes join the enclosing DBOS transaction when one is
    active — the ambient-session hack, replaced by ``_begin`` (Step 3)."""

    def _begin(self):
        try:
            return DBOS.sql_session, False  # join the workflow's transaction
        except AssertionError:
            return super()._begin()


class DbosDispatcher:
    """Drain the outbox onto DBOS queues; each handler runs as a DBOS step.

    ``drain`` must be called from a DBOS workflow context (enqueue requires
    one) — e.g. from a DBOS-instrumented request handler. The handler rebuilds
    the full ``Event`` from the outbox row and re-hydrates the record at that
    version (idempotent replay semantics).
    """

    def __init__(self, store: Store | None = None):
        self.store = store if store is not None else active_store()

    def drain(self, *, queue_name: str | None = None, limit: int = 100) -> int:
        """Enqueue one DBOS step per ready outbox row; delete on enqueue
        (delivery is now DBOS's job — at-least-once, retried by DBOS)."""
        store = self.store if self.store is not None else active_store()
        n = 0
        with Session(store.engine, future=True) as s:
            rows = claim_ready(s, queue=queue_name, limit=limit)
            for row in rows:
                payload = {
                    "url": store.url,
                    "stream": row.stream,
                    "record_id": str(row.record_id),
                    "version": row.version,
                    "kind": row.kind,
                    "delta": row.delta,
                    "handler_id": row.handler_id,
                }
                queue(row.queue).enqueue(_run_handler, payload)
                s.delete(row)
                n += 1
            s.commit()
        return n


@DBOS.step()
def _run_handler(payload: dict) -> None:
    """DBOS step: rebuild the Event and run the subscription handler. The
    payload carries everything serializable; the step re-activates its own
    Store (ContextVars do not cross the DBOS worker boundary)."""
    store = Store(payload["url"], create_tables=False)
    with store:
        cls = stream_class(payload["stream"])
        record = cls.get(uuid.UUID(payload["record_id"]), version=payload["version"])
        event = Event(kind=payload["kind"], record=record, delta=payload["delta"])
        fn = _HANDLERS[payload["handler_id"]]
        fn(event)
