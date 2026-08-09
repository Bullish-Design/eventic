"""Spike 2 (CONCEPT phase 2): the total-order trap and the scan() watermark.

The concept's own gate: *"The gap test (SS3.1) fails on a naive BIGSERIAL and
passes on the real implementation."* This probe builds that test and runs it
against the live Postgres, plus the SQLite exactness claim.

Three candidate mechanisms, all on a mini ``tick`` table with the same
statement shape as the eventic commit path (CAS read, then INSERT, then
COMMIT, concurrent writers):

  M0  naive        seq = BIGSERIAL;            scan = ORDER BY seq, no guard.
  M1  concept      seq = BIGSERIAL, xid = pg_current_xact_id();
                   scan excludes rows with xid >= snapshot xmin.
  M2  sound        seq = xid8 (pg_current_xact_id()); scan excludes rows with
                   seq >= snapshot xmin.  No separate sequence.

Two deterministic interleavings (barriers, not luck):

  Scenario A (the SS3.1 trap as written): A inserts a low seq and holds its
  transaction open; D inserts a higher seq and commits; the scanner sees D and
  checkpoints past A; A commits below the checkpoint.

  Scenario B (the deeper trap this probe finds): the CAS reads and the INSERTs
  interleave so that seq order diverges from xid order -- D's CAS runs first
  (lowest xid) but its INSERT runs after A's, so D's seq is ABOVE A's. D then
  passes the M1 guard (its xid is below the horizon) and the checkpoint
  advances past A's shadowed row; when A's row becomes visible it is below the
  checkpoint.

Expected outcome:
  M0 drops in A (checkpoint advanced past an uncommitted low seq).
  M1 survives neither: in A, D's CAS runs before A's so D's xid is below the
  horizon while D's seq is above A's -- the guard lets D through and the
  checkpoint skips A. In B, the same divergence via the four-writer
  interleaving. The guard is sound only when seq order IS xid order; a
  separate sequence lets them diverge.
  M2 survives both (seq IS the xid, so the guard is exact by construction).

Run: devenv shell -- uv run python .scratch/projects/008-derived-projection/probes/p02_seq_gap.py
"""

from __future__ import annotations

import os
import sqlite3
import threading
from typing import Any

from sqlalchemy import create_engine, text

PG_URL = os.environ.get(
    "EVENTIC_PG_URL", "postgresql+psycopg://postgres:x@127.0.0.1:5432/eventic_spike"
)
TICK = "eventic_spike_tick"
SEQUENCE = f"{TICK}_seq"


def _setup(engine: Any) -> None:
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {TICK}"))
        conn.execute(text(f"DROP SEQUENCE IF EXISTS {SEQUENCE}"))
        # seq is xid8 (native) so every comparison stays in xid8 space: the
        # '::bigint' cast does not exist on xid8.
        conn.execute(
            text(
                f"""
                CREATE TABLE {TICK} (
                    seq    xid8 PRIMARY KEY,
                    xid    xid8 NOT NULL,
                    tag    TEXT NOT NULL
                )
                """
            )
        )
        conn.execute(text(f"CREATE SEQUENCE {SEQUENCE}"))


class Writer:
    def __init__(
        self,
        engine: Any,
        tag: str,
        seq_sql: str,
        cas_gate: threading.Event,
        ins_gate: threading.Event,
        com_gate: threading.Event,
    ) -> None:
        self.engine = engine
        self.tag = tag
        self.seq_sql = seq_sql
        self.cas_gate = cas_gate
        self.ins_gate = ins_gate
        self.com_gate = com_gate
        self.cas_done = threading.Event()
        self.inserted = threading.Event()
        self.committed_ev = threading.Event()
        self.xid: str | None = None
        self.seq: str | None = None

    def run(self) -> None:
        conn = self.engine.connect()
        trans = conn.begin()
        try:
            assert self.cas_gate.wait(timeout=30), f"{self.tag}: cas gate"
            conn.execute(text(f"SELECT 1 FROM {TICK} WHERE tag = 'nope' FOR UPDATE"))
            self.xid = str(conn.execute(text("SELECT pg_current_xact_id()")).scalar())
            self.cas_done.set()

            assert self.ins_gate.wait(timeout=30), f"{self.tag}: ins gate"
            self.seq = str(conn.execute(text(self.seq_sql)).scalar())
            conn.execute(
                text(f"INSERT INTO {TICK} (seq, xid, tag) VALUES (:seq, :xid, :tag)"),
                {"seq": self.seq, "xid": self.xid, "tag": self.tag},
            )
            self.inserted.set()

            assert self.com_gate.wait(timeout=30), f"{self.tag}: com gate"
            trans.commit()
            self.committed_ev.set()
        finally:
            if trans.is_active:
                trans.rollback()
            conn.close()


# (tag, op) steps; op in {"cas", "ins", "com", "scan"}
SCENARIO_A: tuple[tuple[str, str], ...] = (
    ("cas", "D"), ("cas", "A"),   # xid order D < A
    ("ins", "A"),                 # A takes seq=1, holds
    ("ins", "D"), ("com", "D"),   # D takes seq=2, commits
    ("scan", ""),                 # M0: sees D, cp=2; M1/M2: D shadowed, empty
    ("com", "A"),                 # A commits below the checkpoint
    ("scan", ""),                 # M0: A(1) < 2 -> DROPPED
)

SCENARIO_B: tuple[tuple[str, str], ...] = (
    ("cas", "D"), ("cas", "R"), ("cas", "A"), ("cas", "B"),  # xid D<R<A<B
    ("ins", "A"), ("com", "A"),  # A takes seq=100, commits; D still pre-INSERT
    ("scan", ""),                # M0: cp=100; M1/M2: A shadowed, empty
    ("ins", "D"), ("com", "D"),  # D takes seq=101; xid_D below horizon
    ("scan", ""),                # M1: D passes, cp=101; M0: cp=101; M2: cp=xid_D
    ("ins", "R"), ("com", "R"),
    ("ins", "B"), ("com", "B"),
    ("scan", ""),                # M1: A(100) < 101 -> DROPPED
)


def run_mechanism(engine: Any, mechanism: str, scenario: tuple[tuple[str, str], ...]) -> dict[str, Any]:
    _setup(engine)
    if mechanism == "M2":
        seq_sql = "SELECT pg_current_xact_id()"
    else:
        seq_sql = f"SELECT nextval('{SEQUENCE}')"

    tags = sorted({tag for _op, tag in scenario if tag})
    gates = {tag: {g: threading.Event() for g in ("cas", "ins", "com")} for tag in tags}
    writers = {
        tag: Writer(engine, tag, seq_sql, gates[tag]["cas"], gates[tag]["ins"], gates[tag]["com"])
        for tag in tags
    }
    threads = {
        tag: threading.Thread(target=writers[tag].run) for tag in tags
    }
    for t in threads.values():
        t.start()

    seen: list[str] = []
    checkpoint: str | None = None

    def scan() -> None:
        nonlocal checkpoint
        with engine.connect() as conn:
            where = ""
            params: dict[str, Any] = {}
            if checkpoint is not None:
                where += " AND seq > :cp"
                params["cp"] = checkpoint
            if mechanism == "M1":
                where += " AND xid < pg_snapshot_xmin(pg_current_snapshot())"
            elif mechanism == "M2":
                where += " AND seq < pg_snapshot_xmin(pg_current_snapshot())"
            rows = conn.execute(
                text(f"SELECT seq, tag FROM {TICK} WHERE 1=1 {where} ORDER BY seq"),
                params,
            ).all()
            for seq, _tag in rows:
                checkpoint = str(seq)
                seen.append(str(seq))

    for op, tag in scenario:
        if op == "cas":
            gates[tag]["cas"].set()
            assert writers[tag].cas_done.wait(timeout=30), f"{tag}: cas never ran"
        elif op == "ins":
            gates[tag]["ins"].set()
            assert writers[tag].inserted.wait(timeout=30), f"{tag}: insert never ran"
        elif op == "com":
            gates[tag]["com"].set()
            assert writers[tag].committed_ev.wait(timeout=30), f"{tag}: never committed"
        else:
            scan()

    for tag in tags:
        threads[tag].join(timeout=30)
        assert not threads[tag].is_alive(), f"{tag}: writer hung"

    for w in writers.values():
        assert w.seq is not None and w.xid is not None

    with engine.connect() as conn:
        committed = {
            tag: str(seq)
            for seq, tag in conn.execute(text(f"SELECT seq, tag FROM {TICK}"))
        }
    dropped = {tag: seq for tag, seq in committed.items() if seq not in seen}
    return {
        "mechanism": mechanism,
        "committed": committed,
        "seen": seen,
        "checkpoint": checkpoint,
        "dropped": dropped,
    }


def verify_cas_assigns_xid(engine: Any) -> None:
    """Does a zero-row SELECT FOR UPDATE assign an xid? Peeked from another
    connection via pg_stat_activity.backend_xid (which assigns nothing)."""
    probe = engine.connect()
    probe.begin()
    probe.execute(text(f"SELECT 1 FROM {TICK} WHERE tag='nope' FOR UPDATE"))
    pid = probe.execute(text("SELECT pg_backend_pid()")).scalar()
    with engine.connect() as other:
        row = (
            other.execute(
                text(
                    "SELECT backend_xid IS NOT NULL AS has_xid, "
                    "backend_xmin IS NOT NULL AS has_xmin "
                    "FROM pg_stat_activity WHERE pid = :pid"
                ),
                {"pid": pid},
            )
            .mappings()
            .first()
        )
    probe.rollback()
    probe.close()
    assert row is not None
    return bool(row["has_xid"])


def sqlite_exactness() -> None:
    """SQLite claim: BEGIN IMMEDIATE serialises writes, so insertion order IS
    commit order and a plain monotonic counter is exact -- no watermark.
    File database, one connection per writer (WAL): genuinely concurrent."""
    path = "/tmp/eventic_spike_seq.db"
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass

    def exec_one(sql: str) -> None:
        c = sqlite3.connect(path, timeout=10)
        c.execute(sql)
        c.commit()
        c.close()

    exec_one("PRAGMA journal_mode=WAL")
    exec_one(f"CREATE TABLE {TICK} (seq INTEGER PRIMARY KEY, tag TEXT)")

    def insert(tag: str) -> None:
        c = sqlite3.connect(path, timeout=10)
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            f"INSERT INTO {TICK} (seq, tag) VALUES "
            f"((SELECT COALESCE(MAX(seq),0)+1 FROM {TICK}), ?)",
            (tag,),
        )
        c.commit()
        c.close()

    threads = [threading.Thread(target=insert, args=(f"t{i}",)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rows = sqlite3.connect(path).execute(f"SELECT seq, tag FROM {TICK} ORDER BY seq").fetchall()
    seen: list[int] = []
    for seq, _ in rows:
        if seq > (seen[-1] if seen else 0):
            seen.append(seq)
    dropped = [s for s, _ in rows if s not in seen]
    print(f"  committed {len(rows)} rows, scanner saw {len(seen)}, dropped {dropped}")
    assert not dropped, f"SQLite dropped rows: {dropped}"
    print("  OK: SQLite exact -- a monotonic counter needs no watermark.")


def main() -> None:
    engine = create_engine(PG_URL, isolation_level="READ COMMITTED")
    _setup(engine)
    print("== Postgres: does the zero-row CAS assign an xid? ==")
    cas_assigns = verify_cas_assigns_xid(engine)
    print(
        f"  zero-row SELECT FOR UPDATE: xid assigned = {cas_assigns}\n"
        "  (False: creates assign the xid at the INSERT, changes at the CAS --\n"
        "   a mixed workload is exactly where seq order and xid order diverge.)"
    )

    print("\n== Postgres: the deterministic gap interleavings, per mechanism ==")
    results: dict[str, dict[str, Any]] = {}
    for scenario_name, scenario in (("A", SCENARIO_A), ("B", SCENARIO_B)):
        for mechanism in ("M0", "M1", "M2"):
            result = run_mechanism(engine, mechanism, scenario)
            results[f"{mechanism}/{scenario_name}"] = result
            print(
                f"  {mechanism} scenario {scenario_name}: "
                f"committed={result['committed']} seen={result['seen']} "
                f"dropped={result['dropped']}"
            )

    assert results["M0/A"]["dropped"], "M0 must drop in scenario A"
    assert results["M0/B"]["dropped"] == {}, f"M0/B unexpected drop: {results['M0/B']}"
    assert results["M1/A"]["dropped"], "M1 must drop in scenario A"
    assert results["M1/B"]["dropped"], "M1 must drop in scenario B"
    assert results["M2/A"]["dropped"] == {}, f"M2/A unexpected drop: {results['M2/A']}"
    assert results["M2/B"]["dropped"] == {}, f"M2/B unexpected drop: {results['M2/B']}"
    print(
        "  M0 drops in A (the SS3.1 trap); M1 drops in A and B (seq/xid "
        "divergence makes the guard unsound); M2 survives both."
    )

    print("\n== SQLite: exactness under concurrent writers ==")
    sqlite_exactness()

    print(
        "\nFinding: CONCEPT SS3.2 as written ('record pg_current_xact_id() "
        "alongside seq') is unsound -- a separate sequence lets seq order "
        "diverge from xid order, and the guard cannot protect the checkpoint. "
        "The sound form is seq := pg_current_xact_id() (native xid8, no cast), "
        "guard 'seq < pg_snapshot_xmin(pg_current_snapshot())', with "
        "(seq, revision_id) as the tiebreak for batch rows. Backfill keeps "
        "ORDER BY committed_at, revision."
    )
    engine.dispose()


if __name__ == "__main__":
    main()
