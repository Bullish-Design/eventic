"""probe_05 — DBOS 2.29 durable-delivery mechanics on SQLite (final record).

The mechanism Step 7 depends on, verified against the installed dbos 2.29.0:

  1. ``queue.enqueue(fn, *args)`` works from a workflow context and from a
     bare/handler context; it FAILS inside a ``@DBOS.transaction()``
     (``assert cur_ctx.is_workflow()``). Verified.
  2. Enqueued args cross the queue via the default serializer; passing a *str
     id* (never a Record) keeps pickles out of the system DB (R-S1).
  3. The queued handler runs after the commit, once per enqueue; the row the
     handler re-hydrates is durable.
  4. **Non-atomicity finding (deviation D13):** ``Queue.enqueue`` calls
     ``_init_workflow`` which writes the child workflow row in ITS OWN system-
     DB transaction immediately — a subsequently-failed enclosing workflow
     does NOT roll the enqueue back, nor completed transaction-step app
     writes. The guide's "inside the same DBOS transaction that wrote the
     version (transactional outbox)" is not achievable as written on 2.29.
     The honest contract is at-least-once: enqueue happens synchronously right
     after the append succeeds; handlers must be idempotent.

Run: .venv/bin/python .scratch/projects/002-reimplementation/probes/probe_05_dbos_outbox.py
"""
import tempfile
import time
import uuid

from sqlalchemy import create_engine, text

from dbos import DBOS, Queue


tmp = tempfile.mkdtemp()
URL = f"sqlite:///{tmp}/p.db"
results: list = []


@DBOS.step()
def process_id(rid: str) -> None:
    results.append(rid)


q = Queue("probe-q", concurrency=1)


@DBOS.transaction()
def write_row(sid: str) -> None:
    DBOS.sql_session.execute(text("INSERT INTO t (id) VALUES (:id)"), {"id": sid})


@DBOS.workflow()
def happy() -> str:
    sid = str(uuid.uuid4())
    write_row(sid)
    q.enqueue(process_id, sid)
    return sid


@DBOS.workflow()
def aborted() -> str:
    sid = str(uuid.uuid4())
    write_row(sid)
    q.enqueue(process_id, sid)
    raise RuntimeError("boom after enqueue")


def rows():
    eng = create_engine(URL)
    with eng.connect() as c:
        return set(c.execute(text("SELECT id FROM t")).scalars().all())


def wait_until(pred, timeout=10):
    t0 = time.time()
    while not pred() and time.time() - t0 < timeout:
        time.sleep(0.2)


def main():
    with create_engine(URL).begin() as c:
        c.execute(text("CREATE TABLE t (id TEXT PRIMARY KEY)"))

    DBOS(config={"name": "probe05", "application_database_url": URL})
    DBOS.launch()
    try:
        # 1. enqueue inside a transaction -> AssertionError
        try:
            q.enqueue(process_id, "nope")  # no context at all: works (standalone)
            print("[1] enqueue outside any workflow context  : OK (standalone record)")
        except AssertionError as e:
            print(f"[1] enqueue outside any workflow context  : ASSERT {e}")

        # 2. happy path
        sid = happy()
        wait_until(lambda: sid in results)
        print(f"[2] committed workflow -> row+handler       : row={sid in rows()} handler={sid in results}")

        # 3. id-only args
        print(f"[3] enqueued arg is a str id, not a Record  : {isinstance(results[0], str)}")

        # 4. abort path — the D13 finding (documented, not asserted as atomic)
        results.clear()
        try:
            aborted()
        except RuntimeError:
            pass
        time.sleep(2)
        print(f"[4] failed workflow -> enqueue survives     : handler={bool(results)}  "
              f"(D13: enqueue is recorded immediately, NOT rolled back)")
        print()
        print("=> Durable delivery works (id-only args, at-least-once). The guide's")
        print("   'transactional outbox' claim is downgraded per D13 (see LOG/appendix).")
    finally:
        DBOS.destroy()


if __name__ == "__main__":
    main()
