"""DBOS driver tests (Step 11) — gated on the ``eventic[dbos]`` extra.

These start a real DBOS instance, so they are slower than the core suite.
Verified behaviors:

- ``DbosStore`` joins the enclosing DBOS transaction (F3): a save inside a
  transaction that aborts leaves no row and fires no handler.
- A committed DBOS transaction fires handlers strictly post-commit.
- ``DbosDispatcher`` drains the outbox onto DBOS queues; each handler runs as
  a DBOS step and receives the full ``Event``.
"""

import time
import uuid

import pytest

from eventic import Record, on_commit

try:
    from dbos import DBOS

    HAVE_DBOS = True
except ImportError:
    HAVE_DBOS = False

pytestmark = pytest.mark.skipif(not HAVE_DBOS, reason="requires eventic[dbos]")


def _wait_until(pred, timeout=20):
    t0 = time.time()
    while not pred() and time.time() - t0 < timeout:
        time.sleep(0.2)
    return pred()


@pytest.fixture()
def dbos_env(tmp_path):
    """One fresh DBOS instance + a DbosStore per test."""
    from eventic.contrib.dbos import DbosStore

    url = f"sqlite:///{tmp_path / 'e.db'}"
    DBOS(config={"name": "ev-dbos-test", "application_database_url": url})
    DBOS.launch()
    store = DbosStore(url, create_tables=True).activate()
    yield store, url
    DBOS.destroy()
    store.deactivate()


def test_aborted_dbos_txn_leaves_no_row_and_fires_nothing(dbos_env):
    """F3 on the DBOS path: I7 holds — a version that is rolled back never
    fires a handler, and no row survives."""
    from sqlalchemy import func, select

    from eventic.store.schema import LogRow

    store, _ = dbos_env

    class Order2(Record, stream="dbos_abort"):
        total: int = 0

    fired = []

    @on_commit(Order2, kind="create")
    def h_abort(ev):
        fired.append(ev.record.id)

    @DBOS.transaction()
    def abort_txn():
        Order2(total=99).save()
        raise RuntimeError("abort after save")

    with pytest.raises(RuntimeError):
        abort_txn()
    assert fired == []  # I7: never fired for a version that was rolled back
    with store.session() as s:
        n = s.execute(
            select(func.count(LogRow.id)).where(LogRow.stream == "dbos_abort")
        ).scalar()
    assert n == 0


def test_committed_dbos_txn_fires_post_commit(dbos_env):
    """Inside a successful DBOS transaction the durability line still holds:
    the handler sees the row."""
    store, _ = dbos_env

    class Order3(Record, stream="dbos_commit"):
        total: int = 0

    seen = []

    @on_commit(Order3, kind="create")
    def h_commit(ev):
        seen.append(ev.record.id)
        Order3.get(ev.record.id)  # post-commit: must not raise

    @DBOS.transaction()
    def save_txn():
        return Order3(total=5).save().id

    rid = save_txn()
    assert seen == [rid]


def test_outbox_drained_onto_dbos_queue(dbos_env):
    """A durable subscription is staged inside the commit, drained by
    ``DbosDispatcher`` (in a workflow context), and its handler runs as a DBOS
    step with the full Event."""
    store, url = dbos_env

    class Order4(Record, stream="dbos_outbox"):
        total: int = 0

    seen = []

    @on_commit(Order4, via="outbox", queue="orders")
    def h_dur(ev):
        seen.append((ev.kind, ev.record.id, ev.record.total))

    o = Order4(total=7).save()
    assert seen == []  # staged, not delivered

    from eventic.contrib.dbos import DbosDispatcher

    @DBOS.workflow()
    def drain_wf():
        return DbosDispatcher(store).drain()

    assert drain_wf() == 1
    assert _wait_until(lambda: bool(seen)), "durable handler never ran"
    assert seen == [("create", o.id, 7)]


def test_durable_replay_does_not_restage(dbos_env):
    """A byte-identical replay is a silent no-op: no new event, no new outbox
    row, so no double delivery (I7 at the delivery layer)."""
    store, _ = dbos_env

    class Order5(Record, stream="dbos_replay"):
        total: int = 0

    seen = []

    @on_commit(Order5, via="outbox", queue="orders")
    def h_replay(ev):
        seen.append(ev.record.id)

    o = Order5(total=1).save()
    o.save()  # byte-identical replay -> inserts nothing, stages nothing

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from eventic.store.schema import OutboxRow

    with Session(store.engine) as s:
        n = len(s.execute(select(OutboxRow)).scalars().all())
    assert n == 1  # exactly one staged row, despite two save() calls
