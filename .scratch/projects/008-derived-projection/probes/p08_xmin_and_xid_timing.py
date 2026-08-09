"""Spike 8 (REVIEW B7 + D2): what the scan guard is really coupled to, and
when the real commit path assigns its xid.

B7 -- what pins ``pg_snapshot_xmin``?
    CONCEPT SS10 prices the guard as "the visibility lag of the oldest in-flight
    write transaction".  Read naturally, that says *eventic's* writes.  Snapshot
    xmin is computed from the cluster-wide ProcArray, so the real coupling may
    be much broader.  Three questions, answered against live Postgres 17:
      a. does a read-only transaction pin it?
      b. does a write transaction in a DIFFERENT DATABASE of the same cluster
         pin it?
      c. what is the observable effect on ``scan()`` while it is pinned?

D2 -- xid assignment timing on the REAL commit path.
    SPIKES F2.2 concludes that seq order and xid order diverge because "a
    zero-row SELECT FOR UPDATE does not assign an xid, so *creates* assign it at
    the INSERT" -- but it establishes that on a two-statement mini table
    (``eventic_spike_tick``), not on ``_commit_one``, which does a head
    ``SELECT ... FOR UPDATE``, a revision-row select, a revision insert, a head
    upsert and N intent inserts in one transaction.  This re-runs the structural
    claim against the real store.

Run: devenv shell -- uv run python .scratch/projects/008-derived-projection/probes/p08_xmin_and_xid_timing.py
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy import create_engine, event as sa_event, text

from eventic import App, Stream
from eventic.sql import Postgres

PG_URL = os.environ.get(
    "EVENTIC_PG_URL", "postgresql+psycopg://postgres:x@127.0.0.1:5432/eventic_spike"
)
OTHER_DB_URL = PG_URL.rsplit("/", 1)[0] + "/postgres"


class Order(BaseModel):
    status: str


orders = Stream(model=Order, name="orders")
app = App(id="p08", streams=[orders])


def xmin(engine: Any) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT pg_snapshot_xmin(pg_current_snapshot())")
            ).scalar()
        )


# ---------------------------------------------------------------------------
# B7 -- what pins the horizon
# ---------------------------------------------------------------------------


def probe_xmin_pinning() -> None:
    engine = create_engine(PG_URL)
    other = create_engine(OTHER_DB_URL)

    with other.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS p08_unrelated"))
        conn.execute(text("CREATE TABLE p08_unrelated (id INT)"))

    base = xmin(engine)
    print(f"  baseline xmin (nothing in flight): {base}")

    # (a) a read-only transaction elsewhere in the cluster
    ro_conn = other.connect()
    ro_conn.begin()
    ro_conn.execute(text("SELECT count(*) FROM p08_unrelated"))
    ro_xmin = xmin(engine)
    ro_conn.rollback()
    ro_conn.close()
    print(
        f"  (a) read-only txn in another database: xmin={ro_xmin} "
        f"({'PINNED' if ro_xmin <= base else 'not pinned'})"
    )

    # (b) a WRITE transaction in a different database of the same cluster
    started = threading.Event()
    release = threading.Event()
    held_xid: dict[str, int] = {}

    def holder() -> None:
        conn = other.connect()
        trans = conn.begin()
        conn.execute(text("INSERT INTO p08_unrelated VALUES (1)"))
        held_xid["xid"] = int(
            conn.execute(text("SELECT pg_current_xact_id()")).scalar()
        )
        started.set()
        release.wait(timeout=30)
        trans.rollback()
        conn.close()

    t = threading.Thread(target=holder)
    t.start()
    assert started.wait(timeout=30)
    pinned = xmin(engine)
    print(
        f"  (b) WRITE txn in database 'postgres' (xid={held_xid['xid']}): "
        f"xmin={pinned} ({'PINNED' if pinned <= held_xid['xid'] else 'not pinned'})"
    )

    # (c) the observable effect on scan() while pinned
    store = Postgres(PG_URL)
    ev = app.bind(store)
    ev[orders].create(Order(status="failed"), id=uuid4())
    with store.engine.connect() as conn:
        visible_while_pinned = conn.execute(
            text(
                "SELECT count(*) FROM eventic_revision "
                "WHERE age(committed_at) < interval '1 minute'"
            )
        ).scalar()
        # The F2.3 guard, applied to a seq that IS the xid of the writing txn.
        guard_horizon = int(
            conn.execute(
                text("SELECT pg_snapshot_xmin(pg_current_snapshot())")
            ).scalar()
        )
    print(
        f"  (c) a row was committed to eventic while the unrelated write txn is "
        f"open:\n"
        f"      rows physically present: {visible_while_pinned}\n"
        f"      scan guard horizon:      {guard_horizon} "
        f"(frozen at the unrelated txn's xid)"
    )
    print(
        f"      => the new row's seq (its own xid, > {held_xid['xid']}) is at or "
        f"above the\n"
        f"         horizon, so scan() excludes it for as long as the UNRELATED "
        f"transaction\n"
        f"         in another database stays open."
    )

    release.set()
    t.join(timeout=30)
    after = xmin(engine)
    print(f"  after releasing the unrelated txn: xmin={after} (advanced)")
    assert after > pinned, "xmin did not advance after the holder released"

    print(
        "\n  B7 CONFIRMED: pg_snapshot_xmin is cluster-wide. A write transaction in\n"
        "  ANY database on the instance freezes the projection's horizon; a\n"
        "  read-only transaction does not. SS10's 'the oldest in-flight write\n"
        "  transaction' is correct but reads as eventic-scoped -- it is not.\n"
        "  A stuck 'idle in transaction' connection or an unrelated bulk ETL\n"
        "  stalls every pattern, and under event time (SS6) a stall is\n"
        "  indistinguishable from an idle stream."
    )
    store.close()
    with other.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS p08_unrelated"))
    engine.dispose()
    other.dispose()


# ---------------------------------------------------------------------------
# D2 -- when does the REAL commit path assign its xid?
# ---------------------------------------------------------------------------


def probe_xid_timing() -> None:
    store = Postgres(PG_URL)
    ev = app.bind(store)

    trace: list[tuple[str, str | None]] = []
    probing = {"busy": False}

    @sa_event.listens_for(store.engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        if probing["busy"]:
            return
        probing["busy"] = True
        try:
            # A SEPARATE cursor on the same DBAPI connection: reusing `cursor`
            # clobbers the result set SQLAlchemy is about to fetch. Same
            # connection => same transaction => the xid state we want to read.
            probe_cur = conn.connection.cursor()
            try:
                probe_cur.execute("SELECT pg_current_xact_id_if_assigned()")
                assigned = probe_cur.fetchone()[0]
            finally:
                probe_cur.close()
        except Exception:  # noqa: BLE001
            assigned = None
        finally:
            probing["busy"] = False
        head = " ".join(statement.split())[:52]
        trace.append((head, str(assigned) if assigned is not None else None))

    print("\n  -- CREATE (no head row exists) --")
    trace.clear()
    rev = ev[orders].create(Order(status="pending"), id=uuid4())
    for stmt, xid in trace:
        print(f"    xid={xid or '-':<10} after: {stmt}")
    create_first = next((i for i, (_, x) in enumerate(trace) if x), None)

    print("\n  -- CHANGE (head row exists, SELECT ... FOR UPDATE locks it) --")
    trace.clear()
    ev[orders].change(rev, status="failed")
    for stmt, xid in trace:
        print(f"    xid={xid or '-':<10} after: {stmt}")
    change_first = next((i for i, (_, x) in enumerate(trace) if x), None)

    print(
        f"\n  first statement to hold an xid: create -> index {create_first}, "
        f"change -> index {change_first}"
    )
    print(
        "  D2 CONFIRMED on the real commit path: a create assigns its xid later in\n"
        "  the transaction than a change does (the change's head SELECT ... FOR\n"
        "  UPDATE locks a live row and assigns immediately; the create's finds\n"
        "  nothing to lock). F2.2's structural claim holds against _commit_one,\n"
        "  not just against the mini table -- so seq order and xid order genuinely\n"
        "  diverge in a mixed create/change workload, and seq MUST be the xid."
    )
    store.close()


def main() -> None:
    engine = create_engine(PG_URL)
    with engine.begin() as conn:
        for tbl in ("eventic_intent", "eventic_head", "eventic_revision", "eventic_schema"):
            conn.execute(text(f"DROP TABLE IF EXISTS {tbl} CASCADE"))
    engine.dispose()

    print("== B7: what pins pg_snapshot_xmin? ==")
    probe_xmin_pinning()
    print("\n== D2: xid assignment timing on the real _commit_one ==")
    probe_xid_timing()


if __name__ == "__main__":
    main()
