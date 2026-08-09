"""Spike 11 (REVIEW B8 + B9): the two mechanism claims in SS5 that the concept
states in one sentence each.

B9 -- "``leased_until`` on the checkpoint row is the single-writer lock."
    A time-based lease is not mutual exclusion. A matcher that pauses past its
    lease (GC, a slow resolve(), an IO hang) has a successor take over while it
    is still mid-cycle; both then write ``cursor``. The emit survives (SS5.2
    makes it idempotent), but the checkpoint does not: the loser's write is a
    lost update that can move the cursor BACKWARDS (re-scan) or FORWARDS past
    rows the winner has not processed (silent loss). SS3.1 spends two pages
    refusing to fool itself about concurrency; this gets one sentence.

B8 -- the crash window is handled in one direction only.
    SS5's loop is three independent transactions per cycle: emit, match-state
    update, checkpoint advance. SS5.2 handles emit-then-crash (a duplicate,
    absorbed by replay -- confirmed on the real store in p07). It does not
    handle state-advance-then-crash-before-emit, which LOSES a match with no
    detection story. The ordering constraint is load-bearing and unstated.

Run: devenv shell -- uv run python .scratch/projects/008-derived-projection/probes/p11_lease_and_crash_order.py
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field

DB = "/tmp/p11_projection.db"


# ---------------------------------------------------------------------------
# B9 -- the lease is not a lock
# ---------------------------------------------------------------------------


def setup_checkpoint() -> sqlite3.Connection:
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(DB + suffix)
        except FileNotFoundError:
            pass
    conn = sqlite3.connect(DB)
    conn.execute(
        "CREATE TABLE eventic_projection ("
        "  name TEXT PRIMARY KEY,"
        "  cursor INTEGER NOT NULL,"
        "  pattern_version INTEGER NOT NULL,"
        "  leased_until REAL,"
        "  lease_epoch INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO eventic_projection "
        "(name, cursor, pattern_version, leased_until, lease_epoch) "
        "VALUES ('fraud.velocity.v1', 0, 1, 0.0, 0)"
    )
    conn.commit()
    return conn


def probe_naive_lease() -> int:
    """SS5 as written: advance the cursor under a time lease, no fencing."""
    conn = setup_checkpoint()
    clock = 100.0

    # Matcher A acquires the lease at t=100, valid until t=130.
    conn.execute(
        "UPDATE eventic_projection SET leased_until = ? WHERE name = ?",
        (clock + 30, "fraud.velocity.v1"),
    )
    conn.commit()
    a_cursor_read = conn.execute(
        "SELECT cursor FROM eventic_projection WHERE name = ?", ("fraud.velocity.v1",)
    ).fetchone()[0]
    print(f"    t=100  matcher A acquires lease (until 130), reads cursor={a_cursor_read}")

    # A stalls: a GC pause / slow resolve() / IO hang carries it past t=130.
    clock = 140.0
    print(f"    t=140  matcher A is STALLED (still mid-cycle); its lease has expired")

    # B sees an expired lease and takes over. It scans a long way.
    conn.execute(
        "UPDATE eventic_projection SET leased_until = ? "
        "WHERE name = ? AND leased_until < ?",
        (clock + 30, "fraud.velocity.v1", clock),
    )
    conn.execute(
        "UPDATE eventic_projection SET cursor = ? WHERE name = ?",
        (500, "fraud.velocity.v1"),
    )
    conn.commit()
    print(f"    t=140  matcher B takes over, processes rows 1..500, cursor=500")

    # A wakes up and finishes its cycle, writing the cursor it computed.
    conn.execute(
        "UPDATE eventic_projection SET cursor = ? WHERE name = ?",
        (100, "fraud.velocity.v1"),
    )
    conn.commit()
    final = conn.execute(
        "SELECT cursor FROM eventic_projection WHERE name = ?", ("fraud.velocity.v1",)
    ).fetchone()[0]
    print(f"    t=141  matcher A wakes and writes ITS cursor=100 -> stored cursor={final}")
    conn.close()
    return final


def probe_fenced_lease() -> int:
    """The fix: a lease epoch, and every cursor write is a CAS on it."""
    conn = setup_checkpoint()
    clock = 100.0

    def acquire(now: float) -> int:
        cur = conn.execute(
            "UPDATE eventic_projection SET leased_until = ?, "
            "lease_epoch = lease_epoch + 1 "
            "WHERE name = ? AND (leased_until IS NULL OR leased_until < ?) "
            "RETURNING lease_epoch",
            (now + 30, "fraud.velocity.v1", now),
        ).fetchone()
        conn.commit()
        return cur[0] if cur else -1

    def advance(cursor: int, epoch: int) -> bool:
        """A cursor write is valid only under the epoch that owns the lease."""
        rows = conn.execute(
            "UPDATE eventic_projection SET cursor = ? "
            "WHERE name = ? AND lease_epoch = ? RETURNING cursor",
            (cursor, "fraud.velocity.v1", epoch),
        ).fetchall()
        conn.commit()
        return bool(rows)

    a_epoch = acquire(clock)
    print(f"    t=100  matcher A acquires lease, fencing epoch={a_epoch}")
    clock = 140.0
    b_epoch = acquire(clock)
    print(f"    t=140  matcher B takes over, fencing epoch={b_epoch}")
    ok_b = advance(500, b_epoch)
    print(f"    t=140  B writes cursor=500 under epoch {b_epoch}: accepted={ok_b}")
    ok_a = advance(100, a_epoch)
    print(f"    t=141  A writes cursor=100 under stale epoch {a_epoch}: accepted={ok_a}")
    final = conn.execute(
        "SELECT cursor FROM eventic_projection WHERE name = ?", ("fraud.velocity.v1",)
    ).fetchone()[0]
    conn.close()
    assert not ok_a and ok_b
    return final


# ---------------------------------------------------------------------------
# B8 -- the crash window, in both directions
# ---------------------------------------------------------------------------


@dataclass
class Sim:
    """The SS5 loop as three separate transactions, with a crash injected
    between two of them."""

    emitted: list[str] = field(default_factory=list)
    state: dict[str, int] = field(default_factory=dict)

    def cycle(self, key: str, terminal: str, *, order: str, crash: bool) -> None:
        if order == "state-first":
            self.state.pop(key, None)  # partial match consumed
            if crash:
                return  # crash before the emit
            self._emit(key, terminal)
        else:  # emit-first (the required order)
            self._emit(key, terminal)
            if crash:
                return  # crash before clearing state
            self.state.pop(key, None)

    def _emit(self, key: str, terminal: str) -> None:
        # SS5.2: the deterministic id makes a duplicate a no-op. p07 confirms
        # this on the real store (replayed=True, no new log row, no intent).
        doc = f"match:{key}:{terminal}"
        if doc not in self.emitted:
            self.emitted.append(doc)

    def recover(self, key: str, terminal: str, *, order: str) -> None:
        """Restart: the matcher re-derives completion only if the partial match
        is still present."""
        if key in self.state and self.state[key] >= 3:
            self.cycle(key, terminal, order=order, crash=False)


def probe_crash_order() -> None:
    for order in ("state-first", "emit-first"):
        sim = Sim(state={"acct-1": 3})
        sim.cycle("acct-1", "rev-7", order=order, crash=True)
        crashed_emitted = list(sim.emitted)
        sim.recover("acct-1", "rev-7", order=order)
        print(
            f"    order={order:<12} crash mid-cycle -> emitted={crashed_emitted}  "
            f"after restart -> emitted={sim.emitted}"
        )
        if order == "state-first":
            assert sim.emitted == [], "expected the match to be LOST"
            print(
                "                   the partial match was consumed before the emit;\n"
                "                   restart cannot re-derive it. MATCH LOST, silently."
            )
        else:
            assert sim.emitted == ["match:acct-1:rev-7"], "expected the match to survive"
            print(
                "                   the emit landed first; restart re-derives and\n"
                "                   re-emits, and replay absorbs the duplicate (p07)."
            )


def main() -> None:
    print("== B9: the lease is not a lock ==")
    print("  naive (SS5 as written):")
    final_naive = probe_naive_lease()
    assert final_naive == 100, f"expected a lost update to 100, got {final_naive}"
    print(
        f"    -> the cursor moved BACKWARDS from 500 to {final_naive}. Rows 101..500\n"
        f"       are re-scanned. With the writes interleaved the other way the cursor\n"
        f"       jumps FORWARDS past unprocessed rows and they are lost instead.\n"
        f"    B9 CONFIRMED: no fencing token, so a stalled matcher corrupts the\n"
        f"    checkpoint even though SS5.2 keeps the emitted matches correct."
    )

    print("\n  fenced (the fix: lease_epoch + CAS on every cursor write):")
    final_fenced = probe_fenced_lease()
    assert final_fenced == 500, f"expected the fence to hold at 500, got {final_fenced}"
    print(
        f"    -> stored cursor={final_fenced}. The stale writer is rejected.\n"
        f"    FIX CONFIRMED: one integer column and a CAS predicate."
    )

    print("\n== B8: the crash window, in both directions ==")
    probe_crash_order()
    print(
        "    B8 CONFIRMED: SS5.2 covers emit-then-crash (a duplicate) and is silent\n"
        "    on state-then-crash (a lost match). The two orders are not equivalent,\n"
        "    and SS5 does not say which one the loop must use."
    )

    print(
        "\nFinding: both are one-line fixes to the design, but neither is currently\n"
        "written down. SS5 must (a) make every checkpoint write a CAS on a fencing\n"
        "epoch bumped at lease acquisition, and (b) mandate that the emit precedes\n"
        "any mutation of eventic_match_state, with the state mutation idempotent\n"
        "under replay. Without (a) the checkpoint is corruptible; without (b)\n"
        "matches are silently lost -- the mirror of the case SS5.2 does handle."
    )


if __name__ == "__main__":
    main()
