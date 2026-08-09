"""Spike 10 (REVIEW B5): does ``eventic_match_state`` stay bounded?

CONCEPT SS5 adds ``eventic_match_state`` keyed by
``(pattern_id, pattern_version, correlation_key)`` and expires partial matches
in step 4 of the loop.  SS6 chooses **event time**: deadlines compare against
the ``committed_at`` of the row currently being processed, never wall clock,
because wall clock would break replay determinism (I15).

SS6 prices the consequence as affecting negation only:
    "a window never closes while the stream is idle, so a pattern whose
     completion is an *expiry* (negation) stalls until unrelated traffic
     arrives"

That understates it.  Under event time the expiry sweep is driven by arriving
rows, so an idle stream retains *every* open partial match indefinitely --
positive patterns included.  And ``correlate`` is an opaque user lambda that
SS4.2 concedes cannot be validated at ``App`` construction.

Four scenarios, simulated over a synthetic log:
  1. strict contiguity + low-cardinality correlate  (the recommended semantics)
  2. strict contiguity + high-cardinality correlate (the footgun)
  3. idle stream under event time                   (nothing expires)
  4. wall-clock expiry                              (bounded, but breaks I15)

Run: devenv shell -- uv run python .scratch/projects/008-derived-projection/probes/p10_match_state_growth.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

WINDOW = timedelta(minutes=5)
ROW_BYTES = 220  # (pattern_id, version, key, step_index, ids[], opened_at, deadline)


@dataclass
class Row:
    seq: int
    committed_at: datetime
    account: str
    status: str


@dataclass
class Partial:
    step_index: int
    matched: list[int] = field(default_factory=list)
    deadline: datetime = datetime.min.replace(tzinfo=UTC)


def make_log(rows: int, accounts: int, *, span: timedelta) -> list[Row]:
    """Per-account event sequences, so runs of failures actually occur.

    Each account's own k-th event is 'failed' when k % 4 < 3 -- runs of three
    failures separated by one success, so three-strike patterns really fire.
    """
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    step = span / max(rows, 1)
    per_account: dict[str, int] = {}
    log: list[Row] = []
    for i in range(rows):
        account = f"acct-{i % accounts}"
        k = per_account.get(account, 0)
        per_account[account] = k + 1
        log.append(
            Row(
                seq=i,
                committed_at=t0 + step * i,
                account=account,
                status="failed" if k % 4 < 3 else "pending",
            )
        )
    return log


def run_matcher(
    log: list[Row],
    *,
    key_of,
    steps_wanted: int = 3,
    clock: str = "event",
    now_at_replay: datetime | None = None,
) -> tuple[int, int, list[int]]:
    """Strict contiguity, one active partial match per key, no overlap.

    Returns (peak_state_rows, final_state_rows, matched_terminal_seqs).
    """
    state: dict[str, Partial] = {}
    peak = 0
    matched: list[int] = []

    for row in log:
        # -- step 4 of the SS5 loop: expire, against the chosen clock --------
        horizon = row.committed_at if clock == "event" else (now_at_replay or row.committed_at)
        for key in [k for k, p in state.items() if p.deadline < horizon]:
            del state[key]

        key = key_of(row)
        if row.status == "failed":
            partial = state.get(key)
            if partial is None:
                partial = Partial(step_index=0, deadline=row.committed_at + WINDOW)
                state[key] = partial
            partial.step_index += 1
            partial.matched.append(row.seq)
            if partial.step_index == steps_wanted:
                matched.append(row.seq)
                del state[key]
        else:
            # strict contiguity: any non-matching row for this key breaks the run
            state.pop(key, None)

        peak = max(peak, len(state))
    return peak, len(state), matched


def mib(rows: int) -> float:
    return rows * ROW_BYTES / 1024 / 1024


def main() -> None:
    ROWS = 20_000

    print("== 1. Strict contiguity + low-cardinality correlate (100 accounts) ==")
    log = make_log(ROWS, accounts=100, span=timedelta(hours=2))
    peak, final, matched = run_matcher(log, key_of=lambda r: r.account)
    print(
        f"   {ROWS} log rows, 100 distinct keys\n"
        f"   peak state rows={peak}  final={final}  matches={len(matched)}\n"
        f"   peak state size ~{mib(peak):.4f} MiB"
    )
    assert peak <= 100, f"state exceeded the key cardinality: {peak}"
    print(
        "   BOUNDED by distinct active keys, exactly as the recommended semantics\n"
        "   predicts (one active partial match per key, no overlap)."
    )

    print("\n== 2. High-cardinality correlate: state scales with THROUGHPUT ==")
    print("   correlate=lambda c: c.revision.revision_id  -- unique per commit")
    print("   (event-time expiry does bound this while the stream is active --")
    print("    the review's 'unbounded' was wrong for the active case. What it")
    print("    is bounded BY is the point:)")
    for span, label in (
        (timedelta(hours=2), "20k rows over 2h   (~2.8 rows/s)"),
        (timedelta(minutes=20), "20k rows over 20m  (~17 rows/s)"),
        (timedelta(minutes=2), "20k rows over 2m   (~167 rows/s)"),
    ):
        spanned = make_log(ROWS, accounts=100, span=span)
        p, _f, _m = run_matcher(spanned, key_of=lambda r: f"rev-{r.seq}")
        rate = ROWS / span.total_seconds()
        print(
            f"     {label:<34} peak state={p:>6,} rows  "
            f"(~{mib(p):.2f} MiB)  ~= rate x window = {rate * WINDOW.total_seconds():,.0f}"
        )
    print(
        "   State size is ~ (arrival rate x window), NOT a constant. A 5-minute\n"
        "   window at 1k rows/s is ~300k live state rows (~63 MiB) for ONE pattern.\n"
        "   No diagnostic exists: SS4.2 concedes `correlate` is opaque to App\n"
        "   construction, so nothing warns the user before the table is built."
    )

    print("\n== 3. Idle stream under event time (SS6) ==")
    # A burst, then the stream goes quiet. Under event time the sweep is driven
    # by arriving rows, so quiet means no sweep.
    burst = make_log(300, accounts=150, span=timedelta(minutes=1))
    peak3, final3, _ = run_matcher(burst, key_of=lambda r: r.account)
    print(
        f"   150 keys, a 1-minute burst, then the stream goes idle.\n"
        f"   state rows left open when the last row is processed: {final3}"
    )
    print(
        f"   Under event time no further row ever arrives, so no expiry sweep ever\n"
        f"   runs: those {final3} rows persist across the idle period indefinitely --\n"
        f"   for a positive pattern, not just negation. SS6 prices this as a\n"
        f"   negation-only cost; it is not."
    )
    assert final3 > 0

    print("\n== 4. Wall-clock expiry: bounded, but breaks I15 ==")
    replay_a = run_matcher(
        log, key_of=lambda r: r.account, clock="wall",
        now_at_replay=datetime(2026, 1, 1, 0, 30, tzinfo=UTC),
    )
    replay_b = run_matcher(
        log, key_of=lambda r: r.account, clock="wall",
        now_at_replay=datetime(2026, 6, 1, tzinfo=UTC),
    )
    print(f"   replay with now=2026-01-01 00:30 -> {len(replay_a[2])} matches")
    print(f"   replay with now=2026-06-01       -> {len(replay_b[2])} matches")
    assert replay_a[2] != replay_b[2], "expected wall clock to be non-deterministic"
    print(
        "   Two replays of the SAME log produce DIFFERENT match sets.\n"
        "   I15 ('a projection is deterministic under replay') fails. SS6's choice\n"
        "   of event time is therefore correct and must not be traded away to fix\n"
        "   scenario 3."
    )

    print(
        "\nFinding: SS6's event-time choice is right, and it is precisely what makes\n"
        "the state table unbounded on an idle stream. The two together mean the\n"
        "watermark heartbeat is NOT a Phase 6 nicety -- it is the only mechanism that\n"
        "bounds eventic_match_state without breaking I15, so it belongs in Phase 4\n"
        "with the matcher itself. Separately, `correlate` needs a declared\n"
        "cardinality budget and a `projection status` state-size metric: nothing\n"
        "else stops scenario 2, and SS4.2 admits App construction cannot."
    )


if __name__ == "__main__":
    main()
