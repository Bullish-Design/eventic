"""The outbox (Step 10): staged inside the commit transaction, drained by
``OutboxDispatcher`` with a full ``Event``, deleted on success, backed off on
failure. Durable handlers receive the same object sync handlers do.

Record classes are defined inside each test (subscriptions are declarations
that live on the class; a shared module-level class would accumulate them).
"""

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from eventic import OutboxDispatcher, Record, on_commit
from eventic.store.schema import OutboxRow


def test_drain_runs_handler_with_full_event(store):
    class Order(Record, stream="ox_runs"):
        total: int = 0

    seen = []

    @on_commit(Order, via="outbox", queue="orders")
    def h_run(ev):
        seen.append((ev.kind, ev.record.id, ev.record.total))

    o = Order(total=5).save()
    assert seen == []  # staged, not delivered

    n = OutboxDispatcher(store).drain()
    assert n == 1
    assert seen == [("create", o.id, 5)]
    with Session(store.engine) as s:
        assert s.execute(select(OutboxRow)).scalars().all() == []  # deleted


def test_drain_queue_filter(store):
    class Order(Record, stream="ox_filter"):
        total: int = 0

    seen = []

    @on_commit(Order, via="outbox", queue="orders")
    def h_filter(ev):
        seen.append(ev.record.id)

    o = Order(total=1).save()
    assert OutboxDispatcher(store).drain(queue="other") == 0
    assert seen == []
    assert OutboxDispatcher(store).drain(queue="orders") == 1
    assert seen == [o.id]


def test_failed_handler_backs_off_not_deletes(store):
    class Order(Record, stream="ox_backoff"):
        total: int = 0

    @on_commit(Order, via="outbox", queue="orders")
    def h_bad(ev):
        raise RuntimeError("nope")

    Order(total=1).save()
    assert OutboxDispatcher(store).drain() == 1
    with Session(store.engine) as s:
        row = s.execute(select(OutboxRow)).scalar_one()
    assert row.attempts == 1
    # SQLite stores timestamps naive (tz dropped); compare against naive UTC
    naive_now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    assert row.available_at > naive_now - dt.timedelta(seconds=1)


def test_drain_rehydrates_later_version(store):
    class Order(Record, stream="ox_versions"):
        total: int = 0

    seen = []

    @on_commit(Order, via="outbox", queue="orders")
    def h_ver(ev):
        seen.append((ev.record.version, ev.record.total))

    o = Order(total=5).save()
    o = o.update(total=6)  # v1 also staged
    assert OutboxDispatcher(store).drain() == 2
    assert sorted(seen) == [(0, 5), (1, 6)]


def test_outbox_reference_not_copy(store):
    """CONCEPT §7.4: the outbox stores a reference (version_id etc.), not a
    pickled Event — a handler rebuilds the record at that version."""
    class Order(Record, stream="ox_reference"):
        total: int = 0

    seen = []

    @on_commit(Order, via="outbox", queue="orders")
    def h_ref(ev):
        seen.append(ev.record.version)

    o = Order(total=1).save()
    with Session(store.engine) as s:
        row = s.execute(select(OutboxRow)).scalar_one()
    assert row.delta is None
    assert row.version_id == o.version_id
    OutboxDispatcher(store).drain()
    assert seen == [0]


def test_delta_column_in_outbox(store):
    class Order(Record, stream="ox_delta"):
        total: int = 0

    seen = []

    @on_commit(Order, kind="update", via="outbox", queue="orders")
    def h_delta(ev):
        seen.append(ev.delta)

    o = Order(total=1).save()
    o = o.update(total=2)
    OutboxDispatcher(store).drain()
    assert seen == [{"total": 2}]
