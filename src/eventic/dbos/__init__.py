"""Opt-in DBOS adapter (``pip install eventic[dbos]`` only).

Everything here imports ``dbos``; nothing in the core import graph ever
imports this module (I6 — verified by ``test_core_is_dbos_free``).

``DurableEvents`` is the delivery-seam plugin whose transactional outbox
enqueues **ids, never Records** (R-S1). ``durable`` is the explicit DBOS step
registration that replaces the old ``@evented`` magic; ``queue(name)`` hands
out explicit, memoized queue handles (DBOS 2.29 rejects a second ``Queue`` of
the same name); ``create_app`` wires FastAPI + DBOS without an ``Eventic(DBOS)``
subclass.

The durable contract (documented in MIGRATION.md/README): handlers registered
``@on_commit(cls, mode="durable", queue="name")`` run **later**, receive the
**id** as a ``str``, re-hydrate themselves, and must be **idempotent**. The
enqueue is recorded in the enclosing DBOS workflow's recovery record — the
transactional outbox (probe_05: commit → handler runs; workflow abort →
nothing runs). ``queue.enqueue`` requires a *workflow* context (a bare
``@DBOS.transaction()`` cannot enqueue), so durable-mode saves must happen
from a workflow (e.g. a DBOS-instrumented request handler or user workflow);
attempting one inside a transaction raises loudly.
"""

from __future__ import annotations

from typing import Any

from dbos import DBOS, Queue as _DBOSQueue

from ..connect import connect as _connect
from ..errors import EventicError
from ..eventbus import _HANDLERS, _HANDLER_IDS
from ..plugins import Plugin, Seam, register_delivery
from ..plugins.persistence import set_ambient_session_provider

__all__ = ["DurableEvents", "create_app", "durable", "queue"]


# ---------------------------------------------------------------------- #
# ambient-session hook: appends join a DBOS transaction when inside one
# ---------------------------------------------------------------------- #
def _ambient_session():
    """The ambient DBOS sql_session, or None outside a transaction."""
    try:
        return DBOS.sql_session
    except AssertionError:
        return None


set_ambient_session_provider(_ambient_session)


# ---------------------------------------------------------------------- #
# explicit queues
# ---------------------------------------------------------------------- #
_QUEUES: dict[str, _DBOSQueue] = {}


def queue(name: str, *, concurrency: int | None = None) -> _DBOSQueue:
    """Create/reuse an explicit DBOS queue handle (memoized: DBOS 2.29 rejects
    declaring the same queue name twice)."""
    if name not in _QUEUES:
        _QUEUES[name] = _DBOSQueue(name, concurrency=concurrency)
    return _QUEUES[name]


def _reset_queues() -> None:
    _QUEUES.clear()


# ---------------------------------------------------------------------- #
# explicit step registration (replaces @evented)
# ---------------------------------------------------------------------- #
def durable(fn):
    """Register ``fn`` as a DBOS step (== ``DBOS.step()``). Explicit, no magic."""
    return DBOS.step()(fn)


# ---------------------------------------------------------------------- #
# the queued dispatcher: (handler_id, record_id) -> run one handler
# ---------------------------------------------------------------------- #
@DBOS.step()
def _run_handlers(handler_id: str, record_id: str) -> None:
    fn = _HANDLER_IDS.get(handler_id)
    if fn is not None:
        fn(record_id)


class DurableEvents(Plugin):
    """Delivery backend for ``mode="durable"`` — the transactional outbox."""

    seam = Seam.DELIVERY
    provides = {"delivery"}
    requires = {"persistence:transactional"}  # outbox must be atomic with the append
    mode = "durable"

    def deliver(self, event) -> None:
        pending: list[tuple[str, str]] = []  # (handler_id, queue_name)
        for c in type(event.record).__mro__:
            for kind, fn, mode, qname, h_id in _HANDLERS.get(c, []):
                if mode != "durable" or kind not in ("*", event.kind) or not qname:
                    continue
                pending.append((h_id, qname))
        if not pending:
            return  # no durable handlers for this event — nothing to enqueue
        try:
            DBOS.sql_session  # noqa: B018 — are we inside a DBOS transaction?
        except AssertionError:
            pass
        else:
            raise EventicError(
                "durable delivery cannot enqueue inside a @DBOS.transaction(): "
                "queue.enqueue needs a workflow context. Save durable-mode "
                "records from a workflow (e.g. a request handler or "
                "@DBOS.workflow), or use the explicit "
                "queue(name).enqueue(fn, id) pattern after the transaction."
            )
        for h_id, qname in pending:
            # enqueue the ID — never the pickled Record (R-S1)
            queue(qname).enqueue(_run_handlers, h_id, str(event.record.id))


register_delivery(DurableEvents)


# ---------------------------------------------------------------------- #
# FastAPI + DBOS wiring (no Eventic(DBOS) subclass, no process singleton)
# ---------------------------------------------------------------------- #
def create_app(name: str, *, db_url: str, **fastapi_kwargs: Any):
    """One-liner for web apps: FastAPI + DBOS + the eventic engine on one DB."""
    from fastapi import FastAPI

    app = FastAPI(**fastapi_kwargs)
    DBOS(config={"name": name, "application_database_url": db_url}, fastapi=app)
    _connect(db_url)  # eventic's own engine on the same DB (one DB, one driver)
    return app
