# 008 — Derived Projection: Spike Findings

Four spikes, run against the live codebase and a live Postgres 17, per the
concept's own instructions — Phase 3 *"run it early and cheaply"* (the
signature that can invalidate the design), Phase 2's gap test (the
correctness trap), Phase 1's fiddly bit (predicates at plan time), and
Phase 4's central mechanism (idempotent re-emission).

**Verdict: the design survives all four, with two amendments.** Phase 1 and
Phase 4 proceed as designed. Phase 3 proceeds with a delivery-envelope
amendment (§5.1's "zero new code" becomes "a small branch, zero new
machinery"). Phase 2 proceeds with a corrected watermark (§3.2 as written is
empirically unsound; the sound form is `seq := pg_current_xact_id()`).

Evidence: `.scratch/projects/008-derived-projection/probes/p0[1-4]_*.py`,
each runnable and self-asserting.

---

## Summary

| Spike | Probe | Verdict | Effect on the plan |
|---|---|---|---|
| 1. Match envelope (Phase 3) | `p01_match_envelope.py` | Tolerable | + a `MatchEnvelope` delivery type; §5.1 "zero new code" softens to a branch in `worker.py`/`dispatch.py` + `App` validation |
| 2. Total order / `scan()` (Phase 2) | `p02_seq_gap.py` | **§3.2 unsound as written** | `seq` must BE the xid (`pg_current_xact_id()`, native `xid8`), not a separate sequence; guard `seq < snapshot xmin`; tiebreak `(seq, revision_id)` |
| 3. Predicates at plan time (Phase 1) | `p03_predicates_plan.py` | Proceed as designed | Predicate input is a `PredicateView` (kind, changed, state, meta) — not the full `Commit`; one type serves both tiers |
| 4. Idempotent re-emission (Phase 4) | `p04_reemit_idempotent.py` | I14 holds | "wakes no subscriber" is outbox-scoped; inline dispatch re-runs on replay — decide guard vs. document |

---

## Spike 1 — the Match envelope (Phase 3)

The concept's bar: *"if it cannot be made tolerable, that is the finding that
should change the plan."*

**Findings**

- **F1.1 — the persisted document is clean.** `Match(pattern_id,
  pattern_version, correlation_key, steps: tuple[RevisionRef, ...])` builds,
  dumps, and hydrates as a plain frozen pydantic model. References, not
  copies (I3), cost nothing at the document level.
- **F1.2 — `resolve()` cannot live where §5.1 puts it, but the fix is
  small.** A handler receiving `Commit[Match, M]` has no way to reach the
  store: handlers are declared before any `Runtime` exists (I4), and I5 bans
  ambient stores, ContextVars, and methods on the state model. The delivery
  envelope must carry the resolver — a new `MatchEnvelope(match, resolver)`
  with `resolve() -> tuple[StoredRevision, ...]`, built by the matcher
  (inline path) and by `Worker._reconstruct` (outbox path), identified by
  "is this stream an emit target of a `Pattern`?" — derivable from `App`
  declarations. Blast radius measured: `dispatch_inline` + `Worker._reconstruct`
  gain one branch; `App._handler_problems` must accept `MatchEnvelope`
  annotations for emit streams (it rejects them today — probed). No new
  queues, retries, or dead-letter paths: §5.1's "zero new code in
  worker.py/retry.py/dispatch.py" becomes "zero new delivery machinery, a
  few lines of envelope construction".
- **F1.3 — the typing regression is exactly as predicted, and unavoidable.**
  Variadic generics (`Match[*Ts]`) do not build under pydantic 2.11
  (`SchemaError` at class definition), and even with a custom schema hook
  Python's type system has no tuple-map, so `resolve()` cannot return
  per-position typed `Revision`s. The honest shape is
  `resolve() -> tuple[Revision[Any, Any], ...]` — positionally stable, no
  casts, no static checking at the seam. Consumers destructure and narrow.
  The README's "no casts, no registry lookup" claim regresses on this
  surface only, as the concept concedes.

**Decision for Phase 3/4:** proceed. Deliver `MatchEnvelope` (or equivalent)
as the emit-stream delivery envelope, accept the weak typing at the resolve
seam, and note in §5.1 that the envelope change is a small, contained delta
rather than literally zero new code.

---

## Spike 2 — total order and the `scan()` watermark (Phase 2)

The concept's bar: *"the gap test fails on a naive BIGSERIAL and passes on
the real implementation."* Built as a deterministic interleaving (barrier
gates, not luck) on a mini table with the same statement shape as the
eventic commit path (CAS read → INSERT → COMMIT), plus the SQLite exactness
claim, run against live Postgres 17.

**Findings**

- **F2.1 — the §3.1 trap is real and reproducible deterministically.**
  Naive `BIGSERIAL` (mechanism M0): a writer inserts a low seq and holds its
  transaction open; a second writer inserts a higher seq and commits; the
  scanner sees the committed row, checkpoints past the held row, and the held
  row is silently dropped when it commits (scenario A: `dropped={'A': '1'}`).
  Non-deterministic, invisible, breaks I2 without raising — exactly as the
  concept says.
- **F2.2 — §3.2 as written is unsound.** "Record `pg_current_xact_id()`
  alongside seq, exclude anything at or above `pg_snapshot_xmin`" (mechanism
  M1) also drops rows — in **both** scenarios. The reason is structural: seq
  (sequence allocation order) and xid (first-write order) are two different
  total orders. In the eventic commit path they can diverge trivially: the
  CAS read assigns the xid for *changes* (probed: a zero-row `SELECT FOR
  UPDATE` does **not** assign an xid, so *creates* assign it at the INSERT),
  while the seq is allocated at the INSERT. A row whose xid is below the
  snapshot horizon can therefore carry a seq above a shadowed (excluded)
  row's seq; the checkpoint advances past the shadowed row via the passing
  row, and the shadowed row is dropped the moment it commits.
  - Scenario A: `M1 dropped={'A': '1'}` — D's CAS ran before A's, so D's xid
    is below the horizon while D's seq (2) is above A's (1); the guard lets D
    through and the checkpoint skips A.
  - Scenario B: `M1 dropped={'A': '1'}` — the four-writer divergence.
- **F2.3 — the sound form: `seq` must BE the xid.** Mechanism M2 —
  `seq := pg_current_xact_id()` stored as native `xid8` (no cast exists from
  `xid8` to `bigint`; comparisons stay in xid8 space, and
  `'123'::xid8 < pg_snapshot_xmin(pg_current_snapshot())` works) — with the
  guard `seq < pg_snapshot_xmin(pg_current_snapshot())` — drops nothing in
  either scenario. Soundness is by construction: a row that commits later has
  its transaction's xid ≥ the scan's xmin > the checkpoint, so it can never
  appear below the checkpoint. `xid8` is 64-bit, so wraparound is not a
  concern. Batches share one xid: tiebreak `(seq, revision_id)`; rows of one
  batch are mutually atomic (all visible or none), so ties never straddle the
  checkpoint.
- **F2.4 — SQLite exactness confirmed.** 16 concurrent writers (file DB,
  WAL, one connection each, `BEGIN IMMEDIATE`): a plain monotonic counter
  (`MAX(seq)+1`), no watermark, zero drops. Insertion order *is* commit
  order because writes are serialised.
- **F2.5 — backfill still works with xid seqs.** The §10 backfill
  (`ORDER BY committed_at, revision` for pre-existing rows) is compatible:
  backfilled rows get small pseudo-seqs, all below any xid-seq of a new row;
  the migration boundary is a one-time cut, and the guard applies only to
  xid-seqs (old rows are all committed, so they always pass the guard).

**Decision for Phase 2:** implement `seq` as `pg_current_xact_id()` (native
`xid8` on Postgres; a monotonic counter on SQLite), NOT a sequence plus a
recorded xid. Amend §3.2's prose accordingly. The conformance scenario
("never returns a row ordered below one it has already returned, under
concurrent writers") should use the two deterministic interleavings from this
probe as its concurrency scenario — they are the minimal reproducers for M0
and M1, and M2 passes both.

---

## Spike 3 — predicates at plan time (Phase 1)

The concept's bar: *"new tests prove no intent row is written for a filtered
commit."*

**Findings**

- **F3.1 — the shared predicate input cannot be the full `Commit`.** A
  `Revision` requires `committed_at` (database clock, unknown before
  COMMIT), so a predicate over `Commit[T, M]` is not decidable at plan time —
  which is the whole point of tier 1. The shared input must be a narrower
  view: `PredicateView(stream, kind, changed, state, meta)`, built by the
  planner from `(base, planned state)` and by the matcher from a hydrated
  `Commit`. `became("status", "failed")` — `key in changed and new
  state[key] == value` — works unchanged in both sites. One definition, two
  evaluation sites: the unification §4.1 asks for.
- **F3.2 — moving `changed_keys` into planning is a pure consolidation.**
  `_plan_change` already holds `base`; `changed_keys(state_tree(base.state),
  state_tree(new_state))` computed at plan time equals the set the runtime
  computes after commit and the worker reconstructs from the log (probed:
  plan view `changed={'status'}` == matcher view `changed={'status'}`).
  No new machinery.
- **F3.3 — the gate holds.** Filtering the planner's own `intents_for`
  output by the predicate yields no `IntentRequest` for the filtered
  subscription; committing the filtered request through the real store leaves
  the intent table with exactly one row, for the unfiltered sibling
  (`on.everything.v1`), and none for `on.failed.v1`. The inline path applies
  the same predicate in `dispatch_inline`.

**Decision for Phase 1:** proceed. Add `when: Predicate | None` to
`Subscription`, compute `changed` inside `plan_create`/`_plan_change`
(threading it to `intents_for`), and filter in `intents_for` +
`dispatch_inline`. The concept's phrase "a pure function of a `Commit`"
should be read as "a pure function of a `Commit`'s plan-time-decidable
parts".

---

## Spike 4 — idempotent re-emission (Phase 4, §5.2)

The concept's central claim: a crash between emit and checkpoint re-emits the
same deterministic-id document, and the existing replay path absorbs it.

**Findings**

- **F4.1 — I14 holds on the real store, with no new code.** A duplicate
  emission (same deterministic id, same payload) returns `replayed=True`,
  and the log and intent tables are unchanged (`1` row each before and
  after). A different terminal revision (a genuinely new match) emits a new
  row (`replayed=False`, log=2, intents=2) — the deterministic id is
  per-terminal, not a global dedup key.
- **F4.2 — "wakes no subscriber" is outbox-scoped.** The mechanism the
  concept cites (replay returns before the intent insert loop) prevents the
  outbox wake. The **inline** path re-runs the handler on replay:
  `Collection._materialize` dispatches inline regardless of
  `result.replayed` (probed: 1 call on first emit, 2 after the duplicate).
  This is pre-existing replay behaviour for all streams, not new to matches.
  Two options: (a) document inline emit-stream subscribers as at-least-once
  (idempotent handlers, consistent with the existing delivery culture), or
  (b) add a `replayed` guard to skip inline dispatch — a behaviour change to
  all streams that needs its own test. Either is fine; the concept should
  say which.

**Decision for Phase 4:** proceed. Use the deterministic match id exactly as
specified. Resolve F4.2 explicitly (recommend (b) — a replayed guard makes
I9's "runs after COMMIT" honest and costs one condition — with a dedicated
test that ordinary replay no longer re-runs inline handlers).

---

## How to proceed, per phase

| Phase | Go / amend | Concrete change |
|---|---|---|
| 1 Predicates | Go as designed | `PredicateView` input (F3.1); `when=` on `Subscription`; `changed` computed in `plan_create`/`_plan_change` and threaded to `intents_for` (F3.2); filter in `intents_for` + `dispatch_inline` (F3.3) |
| 2 Total order | **Amend §3.2** | `seq := pg_current_xact_id()` native `xid8` (F2.3), guard `seq < pg_snapshot_xmin(pg_current_snapshot())`, tiebreak `(seq, revision_id)`; SQLite: monotonic counter, no watermark (F2.4); conformance concurrency scenario = p02's scenarios A and B (F2.1/F2.2) |
| 3 Match envelope | Go, with amendment | `MatchEnvelope(match, resolver)` delivery type (F1.2); `resolve() -> tuple[Revision[Any, Any], ...]` with caller narrowing (F1.3); `App` validation + worker/dispatch branch for emit streams |
| 4 Matcher | Go as designed | Deterministic match id verified (F4.1); decide replayed guard vs. documented at-least-once inline (F4.2) |
| 5 Operations | Go | unchanged; rebuild determinism rides on the same replay path |

## Open decisions to record in the concept

1. **§3.2 mechanism** — replace the separate-sequence description with
   `seq := pg_current_xact_id()` (F2.2/F2.3). This is the one place the
   concept's mechanism was wrong, and the spike caught it before any
   implementation.
2. **§5.1 envelope** — `MatchEnvelope` is a new delivery type; the "zero new
   code in worker.py/dispatch.py" sentence becomes "zero new delivery
   machinery" (F1.2).
3. **§4.1 predicate input** — "a pure function of a `Commit`" becomes "of
   the plan-time-decidable parts of a `Commit`" (`PredicateView`) (F3.1).
4. **§5.2 "wakes no subscriber"** — outbox-scoped; inline re-runs on replay
   unless a guard is added (F4.2).

## Repo state after the spikes

- Probes: `.scratch/projects/008-derived-projection/probes/p01…p04` — all
  self-asserting, all green.
- Baseline gate: `ruff check`, `ruff format --check`, `basedpyright`,
  `pytest -W error` with the live Postgres: **241 passed, 0 skipped**
  (the 4 CI-only skips run and pass against the live container).
- No source or test files were modified; the spikes are scratch-only.
