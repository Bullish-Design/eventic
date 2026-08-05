"""Durable delivery tests (Step 7) — gated on the ``eventic[dbos]`` extra.

Runs under SQLite (DBOS supports it; no Postgres needed). These tests start a
real DBOS instance + queue workers, so they are slower than the core suite —
they live under ``src/tests/dbos`` and are skipped when ``dbos`` is absent.
"""

import time
import uuid

import pytest

try:
    import dbos  # noqa: F401
    from dbos import DBOS

    import eventic.dbos  # registers the durable delivery backend + ambient hook

    HAVE_DBOS = True
except ImportError:
    HAVE_DBOS = False

from eventic.connect import _reset, connect, engine
from eventic.errors import EventicError
from eventic.eventbus import _reset_handlers, on_commit
from eventic.record import Record

pytestmark = pytest.mark.skipif(not HAVE_DBOS, reason="requires eventic[dbos]")


class Order(Record):
    total: int = 0


def _wait_until(pred, timeout=20):
    t0 = time.time()
    while not pred() and time.time() - t0 < timeout:
        time.sleep(0.2)
    return pred()


@pytest.fixture()
def dbos_env(tmp_path):
    """One fresh DBOS (SQLite app+system DB) + eventic engine per test.
    The ambient-session hook stays registered process-wide (registered once by
    importing eventic.dbos); outside a DBOS transaction it returns None, so it
    is inert in the core/plugin suites."""
    _reset()
    _reset_handlers()
    url = f"sqlite:///{tmp_path / 'e.db'}"
    connect(url)
    DBOS(config={"name": "ev-dbos-test", "application_database_url": url})
    DBOS.launch()
    yield url
    DBOS.destroy()
    _reset()
    _reset_handlers()


def test_durable_handler_runs_after_commit_via_queue(dbos_env):
    """I7 for the durable path: the handler runs after the version commits."""
    seen = []

    @on_commit(Order, mode="durable", queue="orders")
    def handle(order_id):
        seen.append(order_id)

    o = Order(total=5).save()
    assert _wait_until(lambda: bool(seen)), "durable handler never ran"
    assert seen == [str(o.id)]
    # the committed row is visible to the handler's re-hydration
    assert Order.get(o.id).total == 5


def test_durable_handler_gets_id_not_record(dbos_env):
    """R-S1: the queued arg is a str id — no Record crosses the queue."""
    seen = []

    @on_commit(Order, mode="durable", queue="orders")
    def handle(order_id):
        seen.append(order_id)

    o = Order(total=9, meta={"note": "secret"}).save()
    assert _wait_until(lambda: bool(seen))
    rid = seen[0]
    assert isinstance(rid, str)
    assert rid == str(o.id)

    # prove the serialized workflow input holds no Record bytes: the record's
    # domain field name must not appear in the system DB's workflow inputs
    from sqlalchemy import text

    with engine().connect() as c:
        rows = c.execute(
            text("SELECT inputs FROM workflow_status WHERE queue_name = 'orders'")
        ).scalars().all()
    assert rows, "no workflow recorded on the orders queue"
    assert all("total" not in (r or "") for r in rows)
    assert all("secret" not in (r or "") for r in rows)


def test_durable_handler_reruns_idempotently_on_replay(dbos_env):
    """The durable contract: handlers are idempotent; replays re-run them."""
    seen = []

    @on_commit(Order, mode="durable", queue="orders")
    def handle(order_id):
        seen.append(order_id)

    o = Order(total=1).save()
    o.save()  # byte-identical replay -> no new event, no new enqueue (I7)
    assert _wait_until(lambda: len(seen) >= 1)
    time.sleep(1.5)
    assert seen.count(str(o.id)) == 1  # exactly one delivery


def test_durable_delivery_is_at_least_once_across_workflow_abort(dbos_env):
    """D13: the enqueue is recorded synchronously at save time and survives a
    later workflow failure (DBOS 2.29 does not roll enqueues back) — so the
    durable contract is at-least-once and handlers must be idempotent."""
    seen = []

    @on_commit(Order, mode="durable", queue="orders")
    def handle(order_id):
        seen.append(order_id)

    @DBOS.workflow()
    def doomed():
        Order(total=1).save()
        raise RuntimeError("abort after save")

    with pytest.raises(RuntimeError):
        doomed()
    assert _wait_until(lambda: bool(seen)), "enqueue was recorded at save time"
    assert len(seen) == 1  # exactly one delivery for one commit (I7)


def test_transaction_wrapped_durable_save_raises_loudly(dbos_env):
    """enqueue needs a workflow context; inside a transaction it fails loudly
    and nothing persists (no silent half-write)."""
    seen = []

    @on_commit(Order, mode="durable", queue="orders")
    def handle(order_id):
        seen.append(order_id)

    @DBOS.transaction()
    def txn_save():
        return Order(total=3).save().id

    with pytest.raises(EventicError, match="workflow context"):
        txn_save()
    with engine().connect() as c:
        from sqlalchemy import text

        n = c.execute(text("SELECT COUNT(*) FROM records")).scalar()
    assert n == 0
    time.sleep(1)
    assert seen == []


def durable_step(fn):
    """Local helper: register a DBOS step (mirrors eventic.dbos.durable)."""
    return DBOS.step()(fn)


def test_ambient_session_joins_the_transaction(dbos_env):
    """persistence:transactional: a row appended inside a DBOS transaction is
    part of it — a transaction fn that saves then raises leaves NO row (the
    append joined the ambient session, unlike an own-session write)."""
    from sqlalchemy import text

    @DBOS.transaction()
    def txn_save_then_raise():
        Order(total=7).save()
        raise RuntimeError("abort the transaction")

    with pytest.raises(RuntimeError):
        txn_save_then_raise()
    with engine().connect() as c:
        n = c.execute(text("SELECT COUNT(*) FROM records")).scalar()
    assert n == 0  # the join means the failed txn rolled the row back


def test_durable_explicit_pattern_works_end_to_end(dbos_env):
    """The explicit durable pattern (TARGET medium example): a transaction-
    wrapped write + an explicit queue().enqueue(fn, id) in the workflow —
    the handler re-hydrates the committed row by id (R-S1)."""
    from eventic.dbos import queue

    ran = []

    @durable_step
    def manual(oid):
        ran.append((oid, Order.get(uuid.UUID(oid)).total))

    @DBOS.transaction()
    def txn_create():
        return Order(total=7).save().id

    @DBOS.workflow()
    def create_wf():
        oid = txn_create()
        queue("orders").enqueue(manual, str(oid))  # id-only arg (R-S1)
        return oid

    oid = create_wf()
    assert _wait_until(lambda: bool(ran))
    assert ran == [(str(oid), 7)]  # id in, re-hydrated, saw the committed row


def test_core_import_is_dbos_free_subprocess():
    """I6 static guard (final live check lands at Step 9 with the new
    ``eventic/__init__.py`` — the old one still imports DBOS until Step 12)."""
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[2] / "eventic"
    for rel in [
        "connect.py", "errors.py", "eventbus.py", "models.py", "pipeline.py",
        "record.py", "plugins/__init__.py", "plugins/codec.py",
        "plugins/delivery.py", "plugins/identity.py", "plugins/interceptor.py",
        "plugins/persistence.py",
    ]:
        for line in (src / rel).read_text().splitlines():
            if line.strip().startswith(("import dbos", "from dbos", "import fastapi", "from fastapi")):
                raise AssertionError(f"{rel}:{line}")
