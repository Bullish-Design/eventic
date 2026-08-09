# 008 — Derived Projection: second spike round (review findings)

Seven probes (`p05`–`p11`), run against the live codebase, live Postgres 17,
and a second fresh Postgres 17 cluster, testing the review findings that the
first round did not cover. All seven are self-asserting and green.

**Verdict: eight of ten findings confirmed; two were wrong, and correcting them
changed the recommendation in both cases.**

The headline is not any single defect. It is that **the concept's stated
mechanism was wrong in one place (B1) that no amount of reading would have
caught, and the review's own reasoning was wrong in another (B6) that no
amount of reading would have caught either.** Both required measurement.

| # | Finding | Probe | Verdict | Effect on the plan |
|---|---|---|---|---|
| B1 | `seq := pg_current_xact_id()` inverts across `pg_dump`/restore | `p05` | **CONFIRMED — blocker** | `seq` must be `(epoch, xid)` |
| B6 | delta/1 scan read amplification | `p06` | **PARTLY WRONG** | the cost driver is `changed`, not the encoding |
| B2 | intra-batch order is a uuid5 coin flip | `p07` | **CONFIRMED** | add `ordinal`; order `(seq, ordinal)` |
| B3 | match-id poison pill | `p07` | **CONFIRMED — blocker** | pin strict contiguity in §4 |
| B7 | `xmin` pinned cluster-wide by unrelated writes | `p08` | **CONFIRMED** | rewrite §10; add a liveness signal |
| D2 | xid timing on the real commit path | `p08` | **CONFIRMED** | F2.2/F2.3 vindicated on `_commit_one` |
| C1 | predicates cannot be fingerprinted as callables | `p09` | **CONFIRMED — blocker** | Phase 1 ships a combinator algebra |
| B5 | `eventic_match_state` growth | `p10` | **PARTLY WRONG** | bounded while active; heartbeats move to Phase 4 |
| B9 | the lease is not a lock | `p11` | **CONFIRMED** | fencing epoch + CAS on every cursor write |
| B8 | crash window handled in one direction only | `p11` | **CONFIRMED** | mandate emit-before-state-mutation |

---

## B1 — `seq := pg_current_xact_id()` does not survive a restore (`p05`)

**Confirmed. This is a blocker, and it invalidates F2.3's soundness claim.**

F2.3 calls the xid-as-seq mechanism "sound by construction". That argument is
scoped to a single unbroken cluster lifetime, and F2.3 does not say so. The
transaction id counter is cluster-local state; `pg_dump` emits `xid8` columns
through the type's output function, as plain integer literals.

Measured end to end across two live clusters (one representative run — the
absolute counters advance on every run, the shape does not):

```
source cluster counter    14535   ->  rows carry seq 14536..14540
projection cursor                     14540
pg_dump COPY payload                  '14536\tsrc-0'   (literal data)
fresh cluster counter       750
post-restore writes                ->  rows carry seq 755..757
scan(after=14540)                  ->  []
```

Three committed rows are invisible **and always will be**. This is §3.1's own
failure mode — *"non-deterministic, invisible, and it breaks I2 without ever
raising"* — reintroduced by the mechanism intended to prevent it, and strictly
worse: not probabilistic, but total and permanent. Affected paths include
`pg_dump`/restore, DR restore, staging clones, logical replication cutover,
and major-version upgrade via `pg_dumpall`. (`pg_upgrade` preserves the
counter; `pg_dump` does not.)

**The fix is confirmed working in the same probe.** Store `(epoch, seq)`, order
lexicographically, and apply the xmin guard *only to the live epoch* — rows in
a closed epoch predate the reset and are all committed, so they can never
appear below the cursor. Epoch is bumped by a migration-time check:

```sql
pg_snapshot_xmin(pg_current_snapshot()) < (SELECT MAX(seq) FROM eventic_revision)
```

which fired correctly (`live=768 max=14546 reset detected=True`), and the
scan then recovered every lost row.

---

## B6 — the review was wrong: `changed` is the cost, not the encoding (`p06`)

**This finding reversed under measurement, and the correction is more useful
than the original claim.**

The review asserted "up to ~42 row reads per scanned log row." That overstated
round trips — an existing test
(`tests/conformance/test_encodings.py::test_point_read_touches_bounded_rows`)
already proves a delta point read is *one* bounded window query, and the probe
confirms it.

Measured on live Postgres 17, 1000 log rows, 25 aggregates interleaved, page=100:

| strategy | queries/page | rows fetched/row | µs/row | ceiling |
|---|---|---|---|---|
| snapshot, state-only predicate | 1.1 | 1.0 | 27 | ~37,000 rows/s |
| snapshot, `changed` batched per page | 2.1 | 2.0 | 83 | ~12,000 rows/s |
| snapshot, `changed` per row | 98.6 | 2.0 | 458 | ~2,200 rows/s |
| delta/1(every=20), `changed` via window | 101.1 | 16.3 | 624 | ~1,600 rows/s |

The delta encoding costs ~1.4× time and ~8× rows transferred — real, but
modest. **The dominant cost is `changed`**: resolving the predecessor revision
per row costs ~17× throughput against a state-only predicate, because it turns
an O(1)-queries-per-page scan into O(page).

That cost is an implementation choice, not a design constraint. Batching the
predecessor lookups into one query per page recovers most of it (458 → 83
µs/row). But the batched form is only expressible for `snapshot/1`; each delta
predecessor needs its own fold.

**So the finding against the concept is sharper than the review's version:** an
order of magnitude in single-writer throughput sits between the best and worst
implementations of the *same specification*, and §5 specifies neither. §5's
loop must mandate per-page batched predecessor resolution; §10 must state the
ceiling; §11 Phase 2 needs a throughput gate alongside its correctness gate.

Both encodings produced identical match sets (325/1000) — semantics agree.

---

## B2 — intra-batch order is a coin flip (`p07`)

**Confirmed.** Over 400 trials, the tiebreak `(seq, revision_id)` preserved the
user's written order in **47.5%** of cases. Through the real store, a single
`ev.batch()` writing `orders` then `payments` produced two rows sharing one
`committed_at` (one transaction) and scanned back as `['payments', 'orders']` —
inverted.

`revision_id` is `uuid5(NS, f"{stream}:{id}:{rev}")` (I6): deterministic, and
arbitrary as an order. F2.3's reasoning — *"rows of one batch are mutually
atomic, so ties never straddle the checkpoint"* — is true and irrelevant;
atomicity is not the issue, the ordering *within* the tie is.

Unlike the backfill caveat in §10, this applies to **every future write**, and
`Batch` is a first-class API (`runtime.py:179`).

**Fix:** an `ordinal` column assigned from the request's index in
`commit(requests)`; order by `(seq, ordinal)`. A column now, a migration later.

---

## B3 — the match-id poison pill (`p07`)

**Confirmed. Blocker.**

§5.2's id is `uuid5(NS, f"{pattern}:{version}:{key}:{terminal_revision_id}")`.
Replay absorption requires `_is_identical` (`sql/store.py:383`), which compares
the payload **digest** — and the payload contains `steps`. The steps tuple is a
function of the matching *path*, not of the terminal revision.

Against the real store:

```
1. emit match, path (a, b, terminal)                    -> ok
2. re-emit the SAME path (the F4.1 control)             -> replay absorbed, revision=0
3. re-emit with a different intermediate: (a, c, terminal)
   attempt 1: RevisionConflict: row exists with different content
   attempt 2: RevisionConflict: row exists with different content
   attempt 3: RevisionConflict: row exists with different content
```

Same pattern, same version, same key, same terminal revision — and the matcher
is wedged on that correlation key permanently, failing identically on every
restart. I14 does not degrade gracefully; it converts a crash into a hard
permanent failure on a hot key.

This is reachable **because §4 never names a selection semantics.** The probe
also confirms the recommended fix: under strict contiguity with one active
partial match per key and no overlap, replaying from every checkpoint 0..4
reproduces the identical path `[5, 6, 7]`. The path becomes a pure function of
the scan order, the terminal revision determines the steps tuple, and §5.2's id
becomes sound.

---

## B7 / D2 — what the guard is coupled to, and when the xid is assigned (`p08`)

**B7 confirmed.** `pg_snapshot_xmin` is computed from the cluster-wide
ProcArray:

- a **read-only** transaction elsewhere does **not** pin it (the review's
  hedge was right);
- a **write** transaction in a *different database of the same cluster* **does**
  pin it — measured: holder xid 22590, horizon frozen at 22590 until release;
- a row committed to eventic during that window carries a seq at or above the
  horizon, so `scan()` excludes it for as long as the unrelated transaction
  stays open.

§10's *"the visibility lag of the oldest in-flight write transaction"* is
literally correct and reads as eventic-scoped. It is not. A stuck
`idle in transaction` connection, or an unrelated bulk ETL in another database
on the same instance, stalls every pattern — and under event time (§6) a stall
is **indistinguishable from an idle stream**. With watermark heartbeats
deferred to Phase 6, phases 1–5 ship with no way to tell the two apart.

**D2 confirmed on the real commit path**, not the mini table. Tracing
`pg_current_xact_id_if_assigned()` after each statement inside `_commit_one`:

```
CREATE  (no head row)          xid first appears at statement 3 (the revision INSERT)
CHANGE  (head row exists)      xid first appears at statement 1 (head SELECT ... FOR UPDATE)
```

A change's head lock assigns immediately; a create's finds nothing to lock.
F2.2's structural claim holds against the real `_commit_one`, so seq order and
xid order genuinely diverge in a mixed create/change workload — **F2.3's
conclusion that seq must BE the xid is vindicated**, and B1 is a correction to
its durability, not to its logic.

---

## C1 — predicates cannot be fingerprinted as callables (`p09`)

**Confirmed. Blocker for Phase 1, which is the phase §11 calls safe.**

§7 wants *"a pattern whose predicate changed without a version bump is a
declaration error, caught by `eventic schema check`."* Three measured failures
of callable fingerprinting:

- **False negative (closure).** `became("status","failed")` and
  `became("status","cancelled")` share the *same code object* — identical
  bytecode fingerprint `08a3318f4b419d01`. Two different meanings, one
  fingerprint. §7's check would pass a real semantic change.
- **False negative (global).** A predicate capturing a module-level
  `THRESHOLD` changed from 100 to 999: bytecode identical, `__closure__` empty
  both times. **Nothing observable changed at all.**
- **False positive.** Two spellings of one predicate fingerprint differently —
  a pure refactor trips the gate.

A fingerprint with both false negatives and false positives is not a gate.

The probe implements the alternative — a frozen combinator algebra (`Became`,
`Equals`, `AtLeast`, `And`, `Or`) — and shows it is hashable, equal-comparable,
canonically serializable for the ledger and for `projection status`, and
evaluable from one definition at **both** tiers over F3.1's `PredicateView`.

**This is why Phase 1 is a one-way door.** §11 sells it as "roughly a day and
worth doing regardless", but Phase 1 fixes the public `Predicate` type and §7
lives in Phase 5. Ship raw callables and §7 is foreclosed permanently. The
algebra costs perhaps half a day more, now.

---

## B5 — state growth: bounded while active, stranded while idle (`p10`)

**The review's "unbounded" was wrong for the active case.** Event-time expiry
does bound the table while rows keep arriving. What it is bounded *by* is the
finding.

- **Strict contiguity + low cardinality (100 accounts, 20k rows):** peak state
  exactly **100 rows** — the key cardinality, as the recommended semantics
  predicts. 5000 matches fired. Bounded and tiny.
- **High-cardinality correlate:** state scales with **arrival rate × window**,
  not with a constant:

  | load | peak state |
  |---|---|
  | ~2.8 rows/s | 634 rows |
  | ~17 rows/s | 3,801 rows |
  | ~167 rows/s | 15,000 rows |

  Extrapolating: a 5-minute window at 1k rows/s is ~300k live state rows
  (~63 MiB) for **one** pattern. No diagnostic exists — §4.2 concedes
  `correlate` is opaque to `App` construction.
- **Idle stream (the real unbounded case):** 150 keys open when a burst ends,
  and under event time no further row arrives, so **no expiry sweep ever
  runs**. Those rows persist indefinitely — for *positive* patterns, not just
  negation. §6 prices this as a negation-only cost; it is not.
- **Wall clock would fix it and break I15.** The same log replayed with
  `now=2026-01-01 00:30` produced **3900** matches; with `now=2026-06-01`,
  **0**. §6's choice of event time is correct and must not be traded away.

**Consequence:** the watermark heartbeat is not a Phase 6 nicety. It is the
only mechanism that bounds `eventic_match_state` without breaking I15, so it
belongs in **Phase 4, with the matcher itself**. Separately, `correlate` needs
a declared cardinality budget and a `projection status` state-size metric.

---

## B9 / B8 — the lease, and the crash window (`p11`)

**Both confirmed, and both fixes measured working.**

**B9.** §5 calls `leased_until` "the single-writer lock" in one sentence. A
time-based lease is not mutual exclusion. Simulated on a real
`eventic_projection` table:

```
t=100  matcher A acquires lease (until 130), reads cursor=0
t=140  matcher A is STALLED mid-cycle; its lease has expired
t=140  matcher B takes over, processes rows 1..500, cursor=500
t=141  matcher A wakes and writes ITS cursor=100  ->  stored cursor=100
```

The cursor moved **backwards** from 500 to 100; rows 101..500 are re-scanned.
With the writes interleaved the other way it jumps **forwards** past
unprocessed rows and they are lost instead. §5.2 keeps the emitted *matches*
correct under this race; it does nothing for the *checkpoint*.

The fix — a `lease_epoch` bumped at acquisition, with every cursor write a CAS
on it — was measured rejecting the stale writer (`epoch 1: accepted=False`,
`epoch 2: accepted=True`, final cursor 500). One integer column and a
predicate.

**B8.** §5's loop is three independent transactions per cycle. §5.2 handles
emit-then-crash. The mirror case is unhandled:

| order | crash mid-cycle | after restart |
|---|---|---|
| state-first | `emitted=[]` | `emitted=[]` — **match lost, silently** |
| emit-first | `emitted=[match]` | `emitted=[match]` — duplicate absorbed by replay |

Consuming the partial match before emitting means a restart cannot re-derive
completion. The two orders are not equivalent and §5 does not say which the
loop must use.

---

## Revised gate list before Phase 2 starts

| # | Blocker | Status after this round |
|---|---|---|
| 1 | Name the matching semantics in §4 | **Recommendation confirmed**: strict contiguity, one active partial match per key, no overlap — it makes B3 unreachable and bounds B5 to key cardinality |
| 2 | `seq` must be `(epoch, xid)` | **Fix measured working** (`p05` phase 5) |
| 3 | Add `ordinal`; order `(seq, ordinal)` | Confirmed necessary (47.5% coin flip) |
| 4 | Match id must cover the path — or §4 must make the path deterministic | Subsumed by #1 |
| 5 | Emit strictly before match-state mutation | **Confirmed** (`p11`): state-first silently loses the match |
| 6 | Fencing token on the lease | **Confirmed + fix measured** (`p11`): cursor rolled back 500 → 100 without it |
| 7 | Watermark heartbeats → Phase 4; cardinality budget; state-size metric | **Strengthened**: heartbeats are load-bearing, not deferrable |
| 8 | Throughput gate in Phase 2 | **Sharpened**: mandate per-page batched predecessor resolution; the spread is 10× |
| 9 | Move `scan()` to `ProjectionStore` | Unchanged |
| 10 | Rewrite I14; drop the `dispatch_inline` guard | Unchanged |
| 11 | **Phase 1 ships a combinator `Predicate`, not a callable** | **New blocker, and the urgent one** — it gates the phase that ships first |

---

## Repo state

- Probes: `.scratch/projects/008-derived-projection/probes/p05…p11` — all
  self-asserting, all exit 0.
- No source or test files were modified; the spikes are scratch-only
  (`git status`: only `uv.lock`, pre-existing, and this untracked directory).
- Baseline gate with the live Postgres: **244 passed, 0 failed**.
- `p05` requires a second cluster:
  `docker run -d --name eventic-pg-fresh -e POSTGRES_PASSWORD=x -p 55432:5432 postgres:17`
- `p05` and `p08` create scratch tables and drop them on exit. They did not
  originally, and the leftovers made
  `test_alembic_check_clean_on_create_all_database` fail spuriously when
  `EVENTIC_PG_URL` pointed at the same database — worth knowing if a probe is
  ever interrupted mid-run. Cleanup:
  `DROP TABLE IF EXISTS eventic_spike_restore;` (in `eventic_spike`) and
  `DROP TABLE IF EXISTS p08_unrelated;` (in `postgres`).
