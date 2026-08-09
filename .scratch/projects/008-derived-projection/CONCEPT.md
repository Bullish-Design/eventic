# 008 — Record Everything, Respond Selectively (the derived projection)

**Status:** proposed. Extends 1.1; does not supersede `005-redesign/CONCEPT.md`.
**Precondition:** live data exists. Every schema change here needs a migration.
**Invariant references** (`I1`–`I11`) are `docs/INVARIANTS.md`.

---

## 1. The thesis

> **Recording is unconditional and cheap. Responding is a declaration, and it
> may depend on more than one commit.**

Eventic already records everything — the log is append-only and the head is a
derived projection (I1, I2). Nothing on the write side changes. What changes is
that *responding* grows two axes it doesn't have:

| Axis | Today | Proposed |
|---|---|---|
| Which commits | stream name + `kind` | + a pure predicate |
| How many commits | exactly one | + an ordered pattern over N |

The two axes are **not** the same feature, and the central design decision of
this document is to refuse to pretend they are. One is decidable at write time
and stays transactional. The other is not decidable at write time — by
definition — and becomes a derived projection.

---

## 2. The one honest cost, stated first

Today's strongest claim is I8: *the log row, the head row, and every delivery
intent are written in one transaction, or none of them are.*

**A sequence cannot honour that claim.** When commit A arrives, whether it will
ever be followed by B is unknowable. Any construct that fires on "A then B"
must decide *after* B commits, in a separate transaction.

So the proposal is explicitly **two tiers**, named differently in the API so no
one can confuse them:

| Tier | Decided | Guarantee |
|---|---|---|
| `Subscription(when=...)` | at plan time, in the writing process | transactional with the commit (I8 holds) |
| `Pattern(...)` | in the matcher, after the fact | at-least-once, eventually |

If a user needs a transactional guarantee, they must express their condition as
a single-commit predicate. That constraint is a feature; it keeps I8 meaningful
rather than diluting it to "usually."

---

## 3. The prerequisite: a total order, and the trap in it

There is no total order over the log today.

- `eventic_revision` has no global sequence column.
- `committed_at` is *explicitly* not a sort key (I11) — DB precision, ties
  within a batch.
- `revision_id` is `uuid5` — deterministic, not ordered.
- `claim()` orders by `available_at` with `skip_locked` on Postgres
  (`sql/dialect.py:192`), so delivery order is not commit order, not even
  within one aggregate.

A matcher needs to walk the log in commit order. That needs a new column.

### 3.1 The trap

**Do not reach for `BIGSERIAL` and call it done.** Sequence values are
allocated at `INSERT`; rows become visible at `COMMIT`. Therefore:

```
T1  INSERT → seq=100 ─────────────────────────────── COMMIT
T2       INSERT → seq=101 ── COMMIT
                              ↑
                    matcher scans, sees 101, checkpoints at 101
                    seq=100 becomes visible afterwards — never seen
```

The matcher **silently drops** commits under write concurrency. This is the
classic outbox/CDC sequence gap. It is the worst possible failure for this
codebase: non-deterministic, invisible, and it breaks I2 (the log is the only
truth, and projections are rebuildable from it) without ever raising.

Any design here is wrong until this is answered. It is the reason this feature
is a subsystem and not an afternoon.

### 3.2 The answer: `scan()` returns only *stable* rows

Define the contract as a **property**, not a mechanism:

> `scan(after, limit)` never returns a row ordered below any row it has
> previously returned.

Each store satisfies it its own way, and the conformance suite tests the
property under concurrent writers:

- **SQLite** — already fully serialised (`BEGIN IMMEDIATE` at
  `sql/store.py:160`, `concurrent_drainers=False`). Insertion order *is* commit
  order. A plain monotonic counter is exact, no watermark needed.
- **Postgres** — record `pg_current_xact_id()` alongside `seq`, and at scan
  time exclude anything at or above `pg_snapshot_xmin(pg_current_snapshot())`.
  That is exact, not a heuristic; it costs a small visibility lag (the age of
  the oldest in-flight write transaction), not correctness.

This lands well because `Capabilities` is already framed as *"behavior the
conformance suite tests, not marker attributes"* (`protocols.py:28`). Add
`ordered_scan: bool`. A store that cannot offer a stable order simply cannot
host patterns, and `App.bind` says so at bind time — the same shape as the
existing `outbox` capability check (`app.py:148`).

**Rejected alternative:** allocating `seq` from a locked counter row inside the
commit transaction. It gives an exact gap-free order but serialises all
Postgres writes globally, converting per-aggregate concurrency into a single
lock. Too high a price for a store whose production target is Postgres.

---

## 4. The declarations

Frozen values in `App`, like `Stream` and `Subscription` (I4 unchanged).

```python
failed = Step(orders, when=became("status", "failed"))

Pattern(
    id="fraud.velocity.v1",
    version=1,
    steps=(failed, failed, failed),
    correlate=lambda c: c.revision.state.account_id,
    within=timedelta(minutes=5),
    emit=fraud_alerts,          # a Stream[Match]
)
```

### 4.1 The predicate is shared with tier 1

`Step.when` and `Subscription.when` take **the same predicate type**. That is
the unification that makes this one concept rather than two bolted together:
a predicate is a pure function of a `Commit`, and where you place it decides
whether it is evaluated transactionally or in the projection.

`became("status", "failed")` is a helper over `Commit.changed` plus the new
state. Note the plumbing consequence: `changed` is currently computed in
`Collection._materialize` (`runtime.py:136`), *after* the commit, while
`intents_for` runs at plan time (`planning.py:137`). For tier 1 the changed-key
computation must move forward into the planning path. `_plan_change` already
holds `base`, so the information is there — it is a consolidation, not new
machinery, but it is the one fiddly bit of Part 1.

### 4.2 Correlation

`correlate` is a key function returning a hashable. All steps of one partial
match share a key. Cross-stream patterns work as long as each participating
stream can produce the same key — which is exactly the right constraint, and
it is enforceable at `App` construction only loosely (the function is opaque),
so it becomes a runtime error with a good message rather than a config error.

---

## 5. The matcher runtime

One process per pattern group. Loop:

1. `scan(after=checkpoint, limit=N)` — stable rows only, in order.
2. For each row: hydrate to a `Commit`, evaluate steps, advance or open partial
   matches.
3. On completion: **append to the emit stream** through the ordinary commit
   path.
4. Expire partial matches past their deadline.
5. Advance the checkpoint.

Two new tables:

```
eventic_projection    (name PK, cursor, pattern_version, updated_at, leased_until)
eventic_match_state   (pattern_id, pattern_version, correlation_key) PK
                      → step_index, matched_revision_ids, opened_at, deadline
```

`leased_until` on the checkpoint row is the single-writer lock. Sequence
detection is order-sensitive, so it cannot be sharded arbitrarily; it *can* be
sharded by `hash(correlation_key)` later, and the lease generalises to a lease
per shard. Start with one.

### 5.1 Emission is a stream write, and that is the whole trick

The matcher does not invent a new delivery mechanism. It calls
`ev[fraud_alerts].create(...)`. Consequences, all free:

- Matches are **recorded** — append-only, queryable, `history()`-able,
  verifiable. "Record everything" extends to the responses, not just the
  stimuli.
- Ordinary `Subscription`s fire on the emit stream, `Inline` or `Outbox`,
  with the existing worker, retry, dead-letter and redrive machinery untouched.
- Zero new code in `worker.py`, `retry.py`, `dispatch.py`.

The emitted `Match` document holds **references, not copies**:

```python
class Match(BaseModel):
    pattern_id: str
    pattern_version: int
    correlation_key: str
    steps: tuple[RevisionRef, ...]   # (stream, aggregate_id, revision)
```

Copying the matched states into the match document would duplicate the truth
and violate I3 (one canonical document). References keep the log authoritative.
The ergonomic cost — N reads to hydrate a match — is paid by a `resolve()`
helper on the envelope that batches them.

### 5.2 Idempotency comes free from a mechanism you already have

The matcher can crash after emitting but before checkpointing, and will then
re-emit on restart. Make the match's aggregate id deterministic:

```python
uuid5(NS, f"{pattern_id}:{version}:{correlation_key}:{terminal_revision_id}")
```

The re-emit now lands on the **existing replay path** in `_commit_one`
(`sql/store.py:~218`): `_is_identical` matches, the store returns
`replayed=True`, and — critically — it returns *before* the intent insert loop.
So a duplicated emission writes nothing and wakes no subscriber.

This is worth dwelling on: the same deterministic-identity mechanism that makes
`create` replay-safe makes the entire projection idempotent, with no new
concept. It is the strongest signal that the derived-projection shape is the
one this codebase actually wants.

---

## 6. Time, and the subtlest decision in the document

`within=timedelta(...)` needs a clock. The obvious implementation — compare
deadlines against `datetime.now()` in the sweep — makes the projection
**non-deterministic under replay**: rebuilding tomorrow produces a different
set of matches than the live run produced today, because "now" differs. That
silently breaks the rebuildability this whole design rests on.

**Use event time.** Deadlines are compared against the `committed_at` of the
row currently being processed, never against wall clock. Replay is then exact.

The known cost: a window never closes while the stream is idle, so a pattern
whose completion is an *expiry* (negation — "A without B within 5m") stalls
until unrelated traffic arrives. The fix is a **watermark heartbeat** that
advances the matcher clock without inventing events, written to the checkpoint
row so replay sees the same watermarks the live run saw. That is standard
event-time semantics, and it keeps determinism intact.

Recommendation: **ship positive patterns (event time only) first; defer
negation and expiry-triggered matches to a second pass.** Negation is where the
timer complexity actually lives, and it is not needed to prove the shape.

---

## 7. Rebuild and versioning

`eventic projection rebuild --pattern fraud.velocity.v1` truncates match state,
resets the cursor to 0, and replays. Because match ids are deterministic
(§5.2), replay converges on byte-identical output — the projection gets the
same guarantee `heads rebuild` and `verify` already give (I2).

`Pattern.version` participates in the match id. So redeploying a pattern with
changed semantics under a new version emits a *new* set of matches alongside
the old ones; nothing is rewritten, and downstream subscriptions filter by
version. This is the `schema_version` treatment, applied to behaviour instead
of shape, and it should get the fingerprint-ledger treatment too — a pattern
whose predicate changed without a version bump is a declaration error, caught
by `eventic schema check`, not a silent divergence.

---

## 8. Store protocol delta

The core `Store` protocol (`protocols.py:72`, deliberately seven methods) grows
by **one**:

```python
def scan(self, *, after: str | None, limit: int) -> Page[StoredRevision]: ...
```

Partial-match and checkpoint storage go in a **separate narrow protocol**,
`ProjectionStore`, implemented by the SQL store and capability-gated. A minimal
store can therefore implement recording and delivery without implementing
patterns at all — which preserves the `STORE_AUTHORS.md` promise that the
conformance suite is a tractable spec.

New capabilities: `ordered_scan`, `patterns`.

---

## 9. New invariants

Written in the house style — each made true by a mechanism, not a test:

| # | Invariant | Made true by |
|---|---|---|
| **I12** | `scan()` never returns a row ordered below one it has already returned. | SQLite serialises writes (`BEGIN IMMEDIATE`); Postgres excludes rows at or above the snapshot xmin. Conformance tests the property under concurrent writers. |
| **I13** | A match is a recorded document, not an ephemeral callback. | The matcher emits through the ordinary commit path; matches are append-only log rows subject to I1–I3. |
| **I14** | Re-emitting a match is a no-op. | The match aggregate id is `uuid5` over `(pattern, version, key, terminal revision)`; the existing replay path returns `replayed=True` and writes no intents. |
| **I15** | A projection is deterministic under replay. | Windows compare against `committed_at`, never wall clock; watermarks are checkpointed. |

---

## 10. Honest limits

- **Latency.** Matches fire after the scan cycle plus, on Postgres, the
  visibility lag of the oldest in-flight write. Sub-millisecond reaction is not
  on offer.
- **Single writer per pattern.** Throughput is bounded by one matcher until
  correlation-key sharding lands.
- **The `Match` envelope is weakly typed.** `Commit[T, M]` is cleanly generic
  over one revision; a match spans N revisions of possibly different types.
  `steps` is a tuple of references, and `resolve()` returns hydrated revisions
  the caller must narrow. This is a real regression against the README's
  "no casts, no registry lookup" claim, confined to the new surface.
  **Prototype this signature before building anything else** — if it cannot be
  made tolerable, that is the finding that should change the plan.
- **Backfill is approximate.** `seq` cannot be reconstructed in true commit
  order for existing rows; best effort is `ORDER BY committed_at, revision`.
  Existing deployments get an ordering that is correct in the large and
  arbitrary within a tick. Say so in the migration notes.
- **State-per-revision vocabulary.** Patterns match state *transitions*
  ("`status` went `pending` → `failed`"), not named verbs. Workable, and
  arguably better grounded — but it will apply steady pressure to reintroduce
  named domain events. Resist it deliberately or adopt it deliberately; do not
  drift.

---

## 11. Phasing

Each phase ships independently and is useful alone.

| # | Phase | Contents | Gate |
|---|---|---|---|
| 1 | **Predicates** | `when=` on `Subscription`; pull `changed_keys` into planning; filter in `intents_for` and `dispatch_inline` | Existing suite green; new tests prove no intent row is written for a filtered commit |
| 2 | **Total order** | `seq` column + migration + backfill; `scan()`; `ordered_scan` capability; concurrency conformance scenario | The gap test (§3.1) fails on a naive `BIGSERIAL` and passes on the real implementation |
| 3 | **Match envelope spike** | `Match`, `RevisionRef`, `resolve()` — typing only, no runtime | The signature is tolerable, or the plan changes |
| 4 | **Matcher** | `Pattern`, `Step`, `ProjectionStore`, the runtime loop, positive patterns, event time | A three-strike pattern fires once, exactly once, across a matcher restart mid-emit |
| 5 | **Operations** | `projection rebuild`, `projection status`, pattern fingerprints in `schema check` | Rebuild produces byte-identical matches |
| 6 | **Deferred** | Negation, expiry-triggered matches, watermark heartbeats, correlation-key sharding | — |

Phase 1 is roughly a day and is worth doing regardless of whether 2–6 ever
happen. Phase 2 is the one with a correctness trap in it. Phase 3 is the one
that can invalidate the design — run it early and cheaply.
