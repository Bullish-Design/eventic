"""Spike 7 (REVIEW B2 + B3): the two ordering/identity defects in SS5.2's
match id, against the real store.

B2 -- intra-batch order.
    SPIKES F2.3 settles ties within one transaction with
    ``tiebreak (seq, revision_id)``, reasoning that "rows of one batch are
    mutually atomic, so ties never straddle the checkpoint".  Atomicity is not
    the issue: ``revision_id`` is ``uuid5(NS, f"{stream}:{id}:{rev}")`` (I6),
    which is deterministic but *arbitrary* as an order.  Every row written in
    one ``ev.batch()`` shares one xid, so the matcher observes them in uuid5
    hash order -- not in the order the user wrote them.  A two-step pattern
    whose steps are written in a single batch therefore matches or not
    according to a hash.

B3 -- the match-id poison pill.
    SS5.2 makes the match aggregate id
    ``uuid5(NS, f"{pattern}:{version}:{key}:{terminal_revision_id}")`` and
    relies on the store's replay path to absorb a re-emission.  Replay requires
    ``_is_identical`` (sql/store.py:383), which compares the payload **digest**.
    The payload is the ``Match`` document, and it contains ``steps``.  The steps
    tuple is a function of the matching *path*, not of the terminal revision.
    Under any non-strict selection semantics a rescan after a crash may
    legitimately choose a different intermediate revision -- and then the store
    raises ``RevisionConflict("row exists with different content")``
    (sql/store.py:252) on every restart, forever, for that correlation key.

    CONCEPT SS4 never names a selection semantics at all, which is what makes
    this reachable. The probe also shows the recommended fix -- strict
    contiguity, one active partial match per key, no overlap -- yields a path
    that is a pure function of the scan order, so the conflict cannot arise.

Run: devenv shell -- uv run python .scratch/projects/008-derived-projection/probes/p07_batch_order_and_poison.py
"""

from __future__ import annotations

import os
import uuid
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from eventic import App, Stream
from eventic.errors import RevisionConflict
from eventic.ids import NS, revision_id
from eventic.sql import SQLite


# ---------------------------------------------------------------------------
# B2 -- intra-batch order is a uuid5 hash, not the written order
# ---------------------------------------------------------------------------


class Order(BaseModel):
    status: str


class Payment(BaseModel):
    status: str


orders = Stream(model=Order, name="orders")
payments = Stream(model=Payment, name="payments")


def probe_batch_order(trials: int = 400) -> None:
    """Write (order.failed, payment.reversed) in ONE batch, `trials` times.

    Both rows land in one transaction => one xid => the tiebreak decides.
    Count how often the tiebreak order equals the written order.
    """
    agreements = 0
    for _ in range(trials):
        order_id, payment_id = uuid4(), uuid4()
        # Written order: orders first, then payments (revision 0 for both).
        written = [
            ("orders", revision_id("orders", order_id, 0)),
            ("payments", revision_id("payments", payment_id, 0)),
        ]
        by_tiebreak = sorted(written, key=lambda p: p[1])
        if [s for s, _ in by_tiebreak] == [s for s, _ in written]:
            agreements += 1
    rate = agreements / trials
    print(
        f"  {trials} batches of (orders, payments) written in that order:\n"
        f"    tiebreak (seq, revision_id) preserved the written order in "
        f"{agreements}/{trials} = {rate:.1%} of cases"
    )
    assert 0.35 < rate < 0.65, (
        f"expected a coin flip, got {rate:.1%} -- the probe's premise is wrong"
    )
    print(
        "  B2 CONFIRMED: intra-batch order is a coin flip on a uuid5 hash.\n"
        "  A pattern whose steps are written in one ev.batch() matches or not\n"
        "  according to that hash, for every future write, permanently."
    )


def probe_batch_order_live(tmp: str) -> None:
    """The same thing through the real store, to show the rows really do share
    a transaction and really are ordered by the tiebreak."""
    app = App(id="p07b", streams=[orders, payments])
    store = SQLite(tmp)
    ev = app.bind(store)
    order_id, payment_id = uuid4(), uuid4()
    with ev.batch() as b:
        b[orders].create(Order(status="failed"), id=order_id)
        b[payments].create(Payment(status="reversed"), id=payment_id)

    with store.engine.connect() as conn:
        from sqlalchemy import text

        rows = conn.execute(
            text(
                "SELECT stream, revision_id, committed_at FROM eventic_revision "
                "ORDER BY committed_at, revision_id"
            )
        ).all()
    stamps = {str(r[2]) for r in rows}
    order_seen = [r[0] for r in rows]
    print(
        f"    live store: {len(rows)} rows, {len(stamps)} distinct committed_at "
        f"(shared timestamp => one transaction)"
    )
    print(f"    scan order by (committed_at, revision_id): {order_seen}")
    print(f"    written order:                             ['orders', 'payments']")
    if order_seen != ["orders", "payments"]:
        print("    -> this run INVERTED the user's written order")
    store.close()


# ---------------------------------------------------------------------------
# B3 -- the poison pill
# ---------------------------------------------------------------------------


class RevisionRef(BaseModel):
    model_config = ConfigDict(frozen=True)
    stream: str
    aggregate_id: UUID
    revision: int


class Match(BaseModel):
    model_config = ConfigDict(frozen=True)
    pattern_id: str
    pattern_version: int
    correlation_key: str
    steps: tuple[RevisionRef, ...]


matches = Stream(model=Match, name="matches")


def match_id(pattern: str, version: int, key: str, terminal: UUID) -> UUID:
    """CONCEPT SS5.2, verbatim."""
    return uuid.uuid5(NS, f"{pattern}:{version}:{key}:{terminal}")


def probe_poison_pill(tmp: str) -> None:
    app = App(id="p07c", streams=[matches])
    store = SQLite(tmp)
    ev = app.bind(store)

    key = "acct-1"
    terminal = uuid4()
    mid = match_id("fraud.velocity.v1", 1, key, terminal)

    a = RevisionRef(stream="orders", aggregate_id=uuid4(), revision=3)
    b = RevisionRef(stream="orders", aggregate_id=uuid4(), revision=7)
    c = RevisionRef(stream="orders", aggregate_id=uuid4(), revision=9)
    term = RevisionRef(stream="orders", aggregate_id=terminal, revision=11)

    def emit(steps: tuple[RevisionRef, ...]) -> Any:
        return ev[matches].create(
            Match(
                pattern_id="fraud.velocity.v1",
                pattern_version=1,
                correlation_key=key,
                steps=steps,
            ),
            id=mid,
        )

    print("  1. matcher emits a match with path (a, b, terminal)")
    emit((a, b, term))

    print("  2. control -- the SAME path re-emitted after a crash (F4.1)")
    store2 = SQLite(tmp)
    ev2 = App(id="p07c", streams=[matches]).bind(store2)
    result = ev2[matches].create(
        Match(
            pattern_id="fraud.velocity.v1",
            pattern_version=1,
            correlation_key=key,
            steps=(a, b, term),
        ),
        id=mid,
    )
    print(f"     -> no error, revision={result.revision} (replay absorbed it)")
    store2.close()

    print("  3. crash + rescan picks a DIFFERENT intermediate step: (a, c, terminal)")
    print("     same pattern, same version, same key, same terminal revision")
    raised: Exception | None = None
    for attempt in (1, 2, 3):
        try:
            emit((a, c, term))
            print(f"     attempt {attempt}: no error")
        except RevisionConflict as exc:
            raised = exc
            print(f"     attempt {attempt}: RevisionConflict: {exc}")
    assert raised is not None, (
        "expected RevisionConflict -- a differing steps tuple changes the digest"
    )
    print(
        "  B3 CONFIRMED: the match id does not cover the path, so a rescan that\n"
        "  selects a different intermediate revision wedges the matcher on that\n"
        "  correlation key permanently -- it fails identically on every restart."
    )
    store.close()


# ---------------------------------------------------------------------------
# B3 fix -- strict contiguity makes the path a pure function of the scan order
# ---------------------------------------------------------------------------


def strict_contiguity_path(
    log: list[tuple[int, str, str]], key: str, steps_wanted: int
) -> list[int] | None:
    """One active partial match per key, no overlap, reset on a non-matching row.

    Returns the matched path (seq numbers) or None.
    """
    path: list[int] = []
    for seq, k, status in log:
        if k != key:
            continue
        if status == "failed":
            path.append(seq)
            if len(path) == steps_wanted:
                return path
        else:
            path = []  # strict contiguity: any other row breaks the run
    return None


def probe_path_determinism() -> None:
    """Rescan from every possible checkpoint; the path must never change."""
    log = [
        (1, "acct-1", "failed"),
        (2, "acct-2", "pending"),
        (3, "acct-1", "failed"),
        (4, "acct-1", "pending"),  # breaks the run
        (5, "acct-1", "failed"),
        (6, "acct-1", "failed"),
        (7, "acct-1", "failed"),  # terminal of the real match
    ]
    full = strict_contiguity_path(log, "acct-1", 3)
    print(f"  full scan path for acct-1 (3 strikes): {full}")
    assert full == [5, 6, 7]

    # Any crash-and-resume that replays from a checkpoint at or before the
    # start of the winning run must reproduce the identical path.
    for cp in range(0, 5):
        replayed = strict_contiguity_path(
            [r for r in log if r[0] > cp], "acct-1", 3
        )
        assert replayed == full, f"checkpoint {cp} produced {replayed}, not {full}"
    print(
        "  every resume from checkpoints 0..4 reproduces the identical path\n"
        "  FIX CONFIRMED: under strict contiguity + one active partial match per\n"
        "  key, the path is a pure function of the scan order, so the terminal\n"
        "  revision determines the steps tuple and SS5.2's id becomes sound."
    )


def main() -> None:
    for path in ("/tmp/p07_batch.db", "/tmp/p07_poison.db"):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except FileNotFoundError:
                pass

    print("== B2: intra-batch order under tiebreak (seq, revision_id) ==")
    probe_batch_order()
    probe_batch_order_live("/tmp/p07_batch.db")

    print("\n== B3: the SS5.2 match-id poison pill ==")
    probe_poison_pill("/tmp/p07_poison.db")

    print("\n== B3 fix: strict contiguity => path determinism ==")
    probe_path_determinism()

    print(
        "\nFinding: SS5.2's match id is sound only under a selection semantics that\n"
        "makes the path a function of the terminal revision. CONCEPT SS4 names no\n"
        "semantics, so the id is currently unsound. Fix B2 with a per-row `ordinal`\n"
        "assigned from the request index in commit(requests), ordered (seq, ordinal);\n"
        "fix B3 by pinning strict contiguity in SS4, or by folding a path digest into\n"
        "the match id."
    )


if __name__ == "__main__":
    main()
