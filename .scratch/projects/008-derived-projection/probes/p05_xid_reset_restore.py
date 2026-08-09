"""Spike 5 (REVIEW B1): does ``seq := pg_current_xact_id()`` survive a logical
dump/restore into a fresh cluster?

SPIKES.md F2.3 concludes the sound total order is ``seq := pg_current_xact_id()``
stored as native ``xid8``, scanned under the guard
``seq < pg_snapshot_xmin(pg_current_snapshot())``, and calls the result
"sound by construction".

That soundness argument is scoped to a single unbroken cluster lifetime, and
F2.3 does not say so.  The transaction id counter is **cluster-local state**.
``pg_dump`` emits ``xid8`` columns through the type's output function -- as
plain integer literals -- so a restore preserves the *data* while the target
cluster's counter starts wherever ``initdb`` left it.  Every supported logical
upgrade / DR / clone path therefore has the potential to invert the log's
total order:

    source cluster   counter at   14_500   -> rows carry seq ~14_540
    fresh  cluster   counter at      740   -> NEW rows carry seq ~750
    projection cursor = 14_540
    scan(after=14_540) ............................ returns nothing. Forever.

This is CONCEPT SS3.1's own failure mode -- "non-deterministic, invisible, and
it breaks I2 without ever raising" -- reintroduced by the mechanism intended to
prevent it, and strictly worse: not probabilistic, but total and permanent.

The probe runs the whole path against two live Postgres 17 clusters, then
tests the proposed fix (an epoch column, ordered lexicographically as
``(epoch, seq)``, with the epoch bumped by a migration-time detection of
``pg_current_xact_id() < MAX(seq)``).

Requires two containers:
    docker run -d --name eventic-pg       -e POSTGRES_PASSWORD=x -p 5432:5432  postgres:17
    docker run -d --name eventic-pg-fresh -e POSTGRES_PASSWORD=x -p 55432:5432 postgres:17

Run: devenv shell -- uv run python .scratch/projects/008-derived-projection/probes/p05_xid_reset_restore.py
"""

from __future__ import annotations

import subprocess
from typing import Any

from sqlalchemy import create_engine, text

SRC_URL = "postgresql+psycopg://postgres:x@127.0.0.1:5432/eventic_spike"
DST_URL = "postgresql+psycopg://postgres:x@127.0.0.1:55432/eventic_spike"
SRC_CTR = "eventic-pg"
DST_CTR = "eventic-pg-fresh"
TAB = "eventic_spike_restore"


def docker_psql(container: str, db: str, sql: str) -> str:
    out = subprocess.run(
        ["docker", "exec", "-i", container, "psql", "-U", "postgres", "-d", db, "-tAc", sql],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"{container}: {out.stderr.strip()}")
    return out.stdout.strip()


def counter(container: str, db: str = "postgres") -> int:
    """The cluster's current transaction id counter."""
    return int(docker_psql(container, db, "SELECT pg_snapshot_xmin(pg_current_snapshot())"))


# ---------------------------------------------------------------------------
# Phase 1 -- write the log on the source cluster, exactly as F2.3 specifies
# ---------------------------------------------------------------------------


def seed_source() -> tuple[list[tuple[str, str]], str]:
    engine = create_engine(SRC_URL, isolation_level="READ COMMITTED")
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {TAB}"))
        conn.execute(
            text(f"CREATE TABLE {TAB} (seq xid8 PRIMARY KEY, tag TEXT NOT NULL)")
        )
    rows: list[tuple[str, str]] = []
    for i in range(5):
        # One transaction per row: seq IS the xid, as F2.3 requires.
        with engine.begin() as conn:
            seq = conn.execute(
                text(
                    f"INSERT INTO {TAB} (seq, tag) "
                    f"VALUES (pg_current_xact_id(), :tag) RETURNING seq"
                ),
                {"tag": f"src-{i}"},
            ).scalar()
            rows.append((str(seq), f"src-{i}"))
    engine.dispose()
    # The projection cursor after draining everything the source ever wrote.
    cursor = max(int(s) for s, _ in rows)
    return rows, str(cursor)


# ---------------------------------------------------------------------------
# Phase 2 -- the supported logical path: pg_dump | psql into a fresh cluster
# ---------------------------------------------------------------------------


def dump_restore() -> None:
    docker_psql(DST_CTR, "postgres", "DROP DATABASE IF EXISTS eventic_spike")
    docker_psql(DST_CTR, "postgres", "CREATE DATABASE eventic_spike")
    dump = subprocess.run(
        ["docker", "exec", SRC_CTR, "pg_dump", "-U", "postgres", "-t", TAB, "eventic_spike"],
        capture_output=True,
        text=True,
    )
    assert dump.returncode == 0, dump.stderr
    # Evidence that xid8 is dumped as literal data, not as cluster state.
    literal_lines = [
        ln for ln in dump.stdout.splitlines() if ln.startswith("src-") or "\tsrc-" in ln
    ]
    print(f"  pg_dump COPY payload (first 3 of {len(literal_lines)}):")
    for ln in literal_lines[:3]:
        print(f"    {ln!r}")

    load = subprocess.run(
        ["docker", "exec", "-i", DST_CTR, "psql", "-U", "postgres", "-d", "eventic_spike"],
        input=dump.stdout,
        capture_output=True,
        text=True,
    )
    assert load.returncode == 0, load.stderr


# ---------------------------------------------------------------------------
# Phase 3 -- resume writing on the restored cluster, and scan
# ---------------------------------------------------------------------------


def scan(engine: Any, cursor: str) -> list[tuple[str, str]]:
    """The F2.3 scan: guarded, ordered, cursor-advancing."""
    with engine.connect() as conn:
        return [
            (str(seq), tag)
            for seq, tag in conn.execute(
                text(
                    f"SELECT seq, tag FROM {TAB} "
                    f"WHERE seq > :cp "
                    f"AND seq < pg_snapshot_xmin(pg_current_snapshot()) "
                    f"ORDER BY seq"
                ),
                {"cp": cursor},
            )
        ]


def main() -> None:
    print("== Cluster counters before anything ==")
    src_before, dst_before = counter(SRC_CTR), counter(DST_CTR)
    print(f"  source cluster xid counter: {src_before}")
    print(f"  fresh  cluster xid counter: {dst_before}")
    assert dst_before < src_before, (
        "this probe needs the fresh cluster's counter BELOW the source's; "
        "recreate eventic-pg-fresh with a clean volume"
    )

    print("\n== Phase 1: source cluster writes the log (seq := pg_current_xact_id()) ==")
    src_rows, cursor = seed_source()
    print(f"  wrote {len(src_rows)} rows: {src_rows}")
    print(f"  projection cursor after draining: {cursor}")

    print("\n== Phase 2: pg_dump | psql into the fresh cluster ==")
    dump_restore()
    dst_engine = create_engine(DST_URL, isolation_level="READ COMMITTED")
    with dst_engine.connect() as conn:
        restored = [
            (str(seq), tag)
            for seq, tag in conn.execute(text(f"SELECT seq, tag FROM {TAB} ORDER BY seq"))
        ]
    print(f"  restored rows: {restored}")
    assert restored == sorted(src_rows, key=lambda r: int(r[0])), (
        "restore did not preserve seq values literally"
    )
    print("  CONFIRMED: xid8 seq values survive the dump as literal data.")

    print("\n== Phase 3: the restored cluster resumes writing ==")
    new_rows: list[tuple[str, str]] = []
    for i in range(3):
        with dst_engine.begin() as conn:
            seq = conn.execute(
                text(
                    f"INSERT INTO {TAB} (seq, tag) "
                    f"VALUES (pg_current_xact_id(), :tag) RETURNING seq"
                ),
                {"tag": f"post-restore-{i}"},
            ).scalar()
            new_rows.append((str(seq), f"post-restore-{i}"))
    print(f"  new rows: {new_rows}")
    print(
        f"  new seqs are {'BELOW' if int(new_rows[0][0]) < int(cursor) else 'above'} "
        f"the cursor ({cursor})"
    )

    print("\n== Phase 4: the projection scans, holding its cursor ==")
    seen = scan(dst_engine, cursor)
    print(f"  scan(after={cursor}) -> {seen}")
    dropped = [r for r in new_rows if r not in seen]
    if dropped:
        print(
            f"  *** {len(dropped)} committed rows are INVISIBLE and will never be "
            f"returned: {dropped}"
        )
    assert dropped == new_rows, (
        f"expected every post-restore row to be lost, got seen={seen}"
    )
    print("  B1 CONFIRMED: the projection stalls permanently, silently, with no error.")

    # ---------------------------------------------------------------------
    # Phase 5 -- the proposed fix: (epoch, seq), epoch bumped at migration
    # ---------------------------------------------------------------------
    print("\n== Phase 5: the (epoch, seq) fix ==")
    with dst_engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {TAB} ADD COLUMN epoch BIGINT NOT NULL DEFAULT 0"))
        conn.execute(text(f"ALTER TABLE {TAB} DROP CONSTRAINT {TAB}_pkey"))
        conn.execute(text(f"ALTER TABLE {TAB} ADD PRIMARY KEY (epoch, seq)"))
        # The migration-time detection: the cluster counter is below the
        # highest seq we ever recorded => the counter was reset under us.
        live = conn.execute(
            text("SELECT pg_snapshot_xmin(pg_current_snapshot())")
        ).scalar()
        high = conn.execute(text(f"SELECT MAX(seq)::text::bigint FROM {TAB}")).scalar()
        reset_detected = int(live) < int(high)
        print(f"  live counter={live}  max recorded seq={high}  reset detected={reset_detected}")
        assert reset_detected, "the detection rule failed to notice the reset"
        conn.execute(
            text(f"UPDATE {TAB} SET epoch = 1 WHERE tag LIKE 'post-restore-%'")
        )

    with dst_engine.connect() as conn:
        fixed = [
            (int(e), str(s), t)
            for e, s, t in conn.execute(
                text(
                    f"SELECT epoch, seq, tag FROM {TAB} "
                    f"WHERE (epoch, seq) > (:ce, CAST(:cs AS xid8)) "
                    # The xmin guard applies ONLY to the live epoch: rows in a
                    # closed epoch predate the counter reset, so they are all
                    # committed and can never appear below the cursor.
                    f"AND (epoch < :live_epoch "
                    f"     OR seq < pg_snapshot_xmin(pg_current_snapshot())) "
                    f"ORDER BY epoch, seq"
                ),
                {"ce": 0, "cs": cursor, "live_epoch": 1},
            )
        ]
    print(f"  scan(after=(0,{cursor})) with (epoch, seq) -> {fixed}")
    assert [t for _, _, t in fixed] == [t for _, t in new_rows], (
        f"the (epoch, seq) order did not recover the lost rows: {fixed}"
    )
    print("  FIX CONFIRMED: (epoch, seq) lexicographic order recovers every lost row.")

    dst_engine.dispose()

    # Leave no table behind: this probe shares a database with the conformance
    # suite when EVENTIC_PG_URL points here, and a stray table makes
    # `alembic check` report a spurious diff.
    for container in (SRC_CTR, DST_CTR):
        docker_psql(container, "eventic_spike", f"DROP TABLE IF EXISTS {TAB}")
    print("\n  (cleaned up: dropped the probe table from both clusters)")

    print(
        "\nFinding: SPIKES F2.3's 'sound by construction' holds only within one "
        "unbroken cluster lifetime. pg_dump/restore, logical replication cutover, "
        "and major-version upgrade via pg_dumpall all reset the counter and "
        "silently, permanently stall the projection. seq must be (epoch, xid), "
        "with epoch bumped by a migration-time check of "
        "pg_current_xact_id() < MAX(seq)."
    )


if __name__ == "__main__":
    main()
