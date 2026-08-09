# 008 — Derived Projection: Implementation Guide

**Companion to:** `CONCEPT.md`, `SPIKES.md`, and `SPIKES-2.md`.

**Target:** implement selective single-commit subscriptions and deterministic,
positive, multi-commit patterns on top of Eventic 1.1 without weakening I1–I11.

This guide is the implementation authority for project 008. Where the documents
disagree, the precedence is:

1. measured findings in `SPIKES-2.md`;
2. measured findings in `SPIKES.md`;
3. the original mechanism in `CONCEPT.md`.

That ordering matters. In particular, do **not** implement the original
`BIGSERIAL`, separate `seq + xid`, raw-callable predicate, UUID tiebreak,
unfenced lease, state-first mutation, or deferred-heartbeat designs. Each was
disproved or materially amended by a probe.

The work is split into eleven phases (Phase 0 through Phase 10). Every phase has
an outcome, exact code surfaces, required tests, and a binary exit gate. Keep
the repository green after every phase and save each implementation phase as
its own GitMan change.

---

## 0. The contract to freeze before coding

### 0.1 What ships

Project 008 adds two deliberately different response tiers:

| Tier | Declaration | Decision point | Guarantee |
|---|---|---|---|
| Selective subscription | `Subscription(when=...)` | while planning the write | transactional intent creation; I8 remains true |
| Derived pattern | `Pattern(...)` | after commits are visible to the matcher | eventually processed, at-least-once delivery |

The initial pattern release supports only **positive ordered sequences**. It does
not support absence/negation, expiry-triggered emission, overlapping matches,
arbitrary event-time reordering, or correlation-key sharding.

### 0.2 Matching semantics

Pin this behavior in public documentation and tests before implementing the
matcher:

- A pattern has two or more ordered steps.
- There is at most one active partial match per `(pattern id, version,
  correlation key)`.
- Matching is strictly contiguous within a correlation key.
- A row matching the next expected step advances the partial match.
- Any relevant row for the same key that does not match the next expected step
  breaks the partial match. If that row matches step 0, it immediately starts a
  new partial match; otherwise the key becomes inactive.
- Rows from streams not used by the pattern are irrelevant. Rows for other
  correlation keys never break the current key.
- A completed match is removed. Its terminal row is not reused to open an
  overlapping match.
- `within` is measured from the first matched row's `committed_at`. A terminal
  row is accepted when `terminal.committed_at <= opened_at + within`.
- Selection is a pure function of the stable scan order. There are no greedy,
  backtracking, or best-fit alternatives.

These rules are not merely an API preference. They make the matched path
deterministic, prevent the match-id poison pill from `p07`, and bound active
state to one row per active key.

### 0.3 Proposed public surface

Freeze a complete example in a typing fixture and README test:

```python
from datetime import timedelta

from eventic import (
    App,
    Match,
    MatchEnvelope,
    Pattern,
    Step,
    Stream,
    Subscription,
    all_of,
    at_least,
    became,
    state_key,
)

orders = Stream(Order, name="orders")
fraud_alerts = Stream(Match, name="fraud-alerts")

failed = all_of(became("status", "failed"), at_least("amount", 100))

velocity = Pattern(
    id="fraud.velocity",
    version=1,
    steps=(
        Step(orders, when=failed),
        Step(orders, when=failed),
        Step(orders, when=failed),
    ),
    correlate=state_key("account_id"),
    within=timedelta(minutes=5),
    emit=fraud_alerts,
    cardinality_limit=100_000,
)


def handle_alert(envelope: MatchEnvelope) -> None:
    revisions = envelope.resolve()
    assert len(revisions) == 3


app = App(
    id="shop",
    streams=(orders, fraud_alerts),
    subscriptions=(
        Subscription(
            id="notify-one-failure",
            stream=orders,
            when=failed,
            handler=notify_one_failure,
        ),
        Subscription(
            id="notify-velocity",
            stream=fraud_alerts,
            handler=handle_alert,
        ),
    ),
    patterns=(velocity,),
)
```

Do not accept plain predicate callables. `p09` proved that bytecode and closure
fingerprints have both false positives and false negatives.

The same restriction must apply to correlation. A raw `lambda` is just as
unfingerprintable as a raw predicate and would let a pattern's meaning change
without a version bump. Ship a small declarative correlation expression,
starting with `state_key(name)` and `meta_key(name)`. Add callable correlation
only in a later design that supplies a truthful semantic identity.

### 0.4 Corrected invariants

Add these to `docs/INVARIANTS.md` during the final documentation phase, but use
them as implementation constraints from the beginning:

| # | Invariant | Mechanism |
|---|---|---|
| I12 | A projection scan never checkpoints past a row that can later appear below that checkpoint. | Stable `(epoch, transaction sequence, ordinal)` order; the Postgres live epoch is guarded by snapshot xmin; SQLite serializes writes. |
| I13 | A match is an ordinary recorded document, not an ephemeral callback. | The matcher appends `Match` to its declared emit stream through `Runtime`. |
| I14 | Re-emitting the same derived match does not add another log row or outbox intent. Inline handlers remain at-least-once and may run again. | Deterministic match id plus the existing identical-replay path. |
| I15 | Rebuilding a positive pattern from the same ordered log yields byte-identical match documents. | Declarative predicates/correlation, strict-contiguous selection, event time, deterministic ids, and no wall-clock-dependent emissions. |
| I16 | A stale matcher cannot mutate projection state or its cursor after another matcher takes ownership. | Lease epochs and compare-and-swap on every state/cursor transaction. |
| I17 | A crash cannot consume a completed partial match before its durable output exists. | Emit first; then atomically mutate match state and checkpoint. |

### 0.5 Target module layout

Keep the current import layering. The new pure declarations stay above SQL;
SQL implementation stays below the protocol boundary.

```text
src/eventic/
  predicates.py          # PredicateView and frozen predicate/key algebra
  patterns.py            # Step, Pattern, Match, RevisionRef, fingerprints
  projection.py          # matcher runtime; depends on protocols, not SQLAlchemy
  envelopes.py           # MatchEnvelope in addition to Revision/Commit/Page
  wire.py                # scan cursors/pages, leases, state mutations/reports
  protocols.py           # unchanged Store + separate ProjectionStore/Admin
  sql/
    tables.py            # scan clock, order columns, projection/state/ledger tables
    statements.py        # stable scan and fenced projection statements
    store.py             # ordered scan implementation
    projection.py        # SQL ProjectionStore implementation if store.py gets too large
    admin.py             # rebuild/status/fingerprint operations
    migrations/versions/0002_derived_projection.py
  cli/
    main.py              # matcher and projection command parsers
    commands.py          # process ownership, output, exit codes
```

Do not add projection methods to the seven-method `Store` protocol. The second
review explicitly moved `scan()` to `ProjectionStore`; small recording-only
stores must remain valid.

### Phase 0 tests

- Keep the public example as a non-executed fixture in this project directory;
  move it into `tests/typing` and the README when its declarations land in
  Phases 5–6.
- Write the behavior cases for strict contiguity, no overlap, inclusive
  `within`, and reset/restart-on-step-zero as tables of inputs and expected
  outputs. Turn them into executable tests with the pure transition core in
  Phase 8.
- Write the intended import edges down now; add mechanical architecture
  assertions as each new module is introduced.

### Phase 0 exit gate

The API fixture and semantics are reviewed and frozen. No database or matcher
runtime code has been added yet.

---

## Phase 1 — Predicates are durable values

**Outcome:** one predicate definition can be evaluated during planning and
during projection replay, canonically serialized, compared, and fingerprinted.

### 1.1 Add `predicates.py`

Define a frozen, slotted `PredicateView` containing only plan-time-decidable
data:

```python
@dataclass(frozen=True, slots=True)
class PredicateView:
    stream: str
    kind: Kind
    changed: frozenset[str]
    state: JsonObject
    meta: JsonObject
```

It intentionally excludes `revision_id`, aggregate revision, and
`committed_at`; those values do not exist when transactional intents are
planned.

Define a closed predicate algebra. The initial useful set is:

- `Always`
- `Became(key, value)`
- `Equals(key, value)`
- `AtLeast(key, number)`
- `KindIs(kind)`
- `And(terms)`
- `Or(terms)`
- `Not(term)`

Expose constructor helpers (`became`, `equals`, `at_least`, `kind_is`,
`all_of`, `any_of`, `not_`) rather than asking users to instantiate internal
nodes.

Each node must provide exactly two operations:

```python
def evaluate(self, view: PredicateView) -> bool: ...
def canonical(self) -> JsonObject: ...
```

Validate every literal as canonical JSON at declaration time. Reject NaN,
infinity, mutable containers, arbitrary Python objects, and empty `And`/`Or`
terms. Normalize `And` and `Or` by flattening nested nodes, sorting terms by
canonical bytes, and removing exact duplicates. This keeps fingerprints stable
when commutative terms are reordered.

`Became` uses the existing top-level meaning of `Commit.changed`:

```python
key in view.changed and view.state.get(key) == value
```

Do not silently expand this to nested JSON paths. That would change the meaning
of `changed` and needs its own design/version.

### 1.2 Add declarative correlation expressions

In the same module, define a separate `Correlation` algebra:

- `StateKey(key)` / `state_key(key)`
- `MetaKey(key)` / `meta_key(key)`

Evaluation returns a canonical scalar (`str`, `int`, UUID serialized as text,
or another explicitly supported scalar), never a mapping/list. Encode the
stored key as canonical JSON text so `1`, `"1"`, and `true` cannot collide.
Enforce a documented byte limit before it reaches a database index.

Missing or unsupported values are projection errors that name the pattern,
step, revision id, and expression. They must stop the projection without
advancing its cursor; never skip a poison row silently.

### 1.3 Fingerprinting

Use `canonical_bytes(predicate.canonical())` and SHA-256. Do not inspect Python
source, bytecode, function names, globals, or closure cells.

Give the algebra an explicit format version such as `predicate/1` and include
it in canonical output. A future semantic change creates `predicate/2`; it
must not silently reinterpret stored pattern fingerprints.

### Phase 1 tests

- Port the false-negative and false-positive cases from `p09` as permanent
  regression tests.
- Prove semantically different values produce different fingerprints.
- Prove repeated declarations and reordered `And`/`Or` terms produce the same
  fingerprint.
- Evaluate every operator against both a planner-built and matcher-built view.
- Property-test canonical round trips over the supported JSON literal domain.
- Test correlation type distinctions, missing keys, size limits, and canonical
  equality.
- Add typing tests showing helpers return the closed `Predicate` and
  `Correlation` types, not callables.

### Phase 1 exit gate

The pure predicate/correlation suite is green with no store import from
`predicates.py`.

---

## Phase 2 — Selective subscriptions remain transactional

**Outcome:** `Subscription(when=...)` filters both inline dispatch and outbox
intent creation from the same predicate and the same planned view.

### 2.1 Declaration changes

Modify `subscription.py`:

```python
@dataclass(frozen=True)
class Subscription[T: BaseModel, M: BaseModel]:
    ...
    when: Predicate = Always()
```

Prefer `Always()` over `None` internally so every dispatch site performs the
same unconditional `evaluate` call. Preserve the existing default behavior
exactly.

Validate that predicate state keys exist on the subscription's stream model
when the model shape permits a definitive answer. Report all declaration
problems through the existing aggregate `App` validation rather than raising at
the first subscription.

### 2.2 Move `changed` into planning

Change `plan_create` and `_plan_change` in `planning.py`:

- canonicalize the new state once;
- obtain its `JsonObject` once;
- compute `changed` before constructing `CommitRequest`;
- create one `PredicateView` from stream, kind, changed, state, and meta;
- pass that view to `intents_for`.

For create, `changed` is every top-level key. For change/replace, compare the
canonical tree of `base.state` with the new canonical tree using the existing
`changed_keys` function.

Change `intents_for` to require the view instead of reconstructing any of this
information. It writes an `IntentRequest` only when stream, kind, delivery, and
predicate all match.

Avoid adding `changed` to `CommitRequest` unless a measured downstream need
appears. It is a planning value, not durable wire state.

### 2.3 Inline dispatch

`runtime.Collection._materialize` already builds the post-commit `Commit` and
its `changed` set. Update `dispatch_inline` to construct the equivalent
`PredicateView` and evaluate `sub.when` before calling the handler.

Do not add the first-round suggestion to suppress inline delivery whenever
`CommitResult.replayed` is true. Existing inline semantics are at-least-once,
and project 008 should not make a global replay behavior change. I14 is scoped
to duplicate log rows and outbox intents; matcher documentation must tell
inline handlers to remain idempotent.

### 2.4 App inspection

Include canonical predicate JSON in `eventic inspect` output. This makes a
resolved declaration observable without relying on `repr()`.

### Phase 2 tests

- A false outbox predicate writes no `eventic_intent` row in the same commit
  that writes the revision/head.
- A true predicate writes exactly one intent.
- Inline and outbox paths make the same decision for create, change, replace,
  replay, and batch writes.
- Two subscriptions on one stream can independently pass/fail.
- A filtered subscription does not weaken the atomic log/head/intent behavior
  of unfiltered siblings.
- Existing applications without `when` behave byte-for-byte as before.
- Update unit, conformance, typing, README, and inspect-output fixtures.

### Phase 2 exit gate

The full existing suite plus selective-subscription tests pass. This phase can
ship independently, but only with the value-based predicate API from Phase 1.

---

## Phase 3 — A durable total order over future commits

**Outcome:** every new revision has a stable lexicographic position that
preserves transaction visibility and caller order within `Runtime.batch()`.

### 3.1 The cursor type

Add frozen wire values in `wire.py`:

```python
@dataclass(frozen=True, order=True, slots=True)
class ScanCursor:
    epoch: int
    sequence: int
    ordinal: int


@dataclass(frozen=True, slots=True)
class ScannedRevision:
    cursor: ScanCursor
    revision: StoredRevision
    changed: frozenset[str]


@dataclass(frozen=True, slots=True)
class ScanPage:
    items: tuple[ScannedRevision, ...]
    cursor: ScanCursor | None
    stable_horizon: ScanCursor | None
```

Keep the cursor opaque at the CLI boundary by encoding its three integers as
versioned base64url JSON. Internal code should use the typed value.

### 3.2 Schema changes

Add a `0002_derived_projection.py` migration and matching table metadata.

`eventic_revision` gains:

```text
scan_epoch       INTEGER NOT NULL
scan_sequence    xid8 on Postgres; INTEGER on SQLite
scan_ordinal     INTEGER NOT NULL
UNIQUE(scan_epoch, scan_sequence, scan_ordinal)
INDEX(scan_epoch, scan_sequence, scan_ordinal)
```

Do not order by `revision_id`. `p07` measured that as a coin flip relative to
the caller's batch order.

Add a singleton `eventic_scan_clock` table:

```text
clock_id          INTEGER PRIMARY KEY, fixed to 1
current_epoch     INTEGER NOT NULL
next_sequence     INTEGER NULL       # used by SQLite only
updated_at        TIMESTAMPTZ NOT NULL
```

The singleton is also a low-frequency barrier. Every Eventic write transaction
takes a **shared** row lock before it reads the epoch and database clock;
concurrent writers remain concurrent because shared locks are compatible.
Epoch transitions and idle-watermark heartbeats take an **exclusive** row lock,
which waits for earlier Eventic writers and prevents later ones until the
transition is durable. This closes two otherwise silent races: a write becoming
visible in a closed epoch, and a heartbeat expiring state ahead of an in-flight
Eventic commit.

For Postgres, define a small SQLAlchemy type for native `xid8`; do not silently
store it as an ordinary autoincrement integer. All xmin comparisons remain in
native xid8 space. Convert to Python `int` only at the wire boundary.

### 3.3 Write-path assignment

Enumerate `Store.commit(requests)` in caller order.

- **Postgres:** acquire the scan-clock shared lock, then read the current epoch
  and database time with `clock_timestamp()`; use `pg_current_xact_id()` as
  `scan_sequence` for every inserted revision; use the request index as
  `scan_ordinal`. Do not use transaction-start `now()` after waiting on the
  barrier: a transaction may have begun before a heartbeat and acquired its
  shared lock afterward, so its durable event time must be captured after the
  lock is granted.
- **SQLite:** while already inside `BEGIN IMMEDIATE`, increment the singleton
  counter once per batch, use that value as `scan_sequence`, and use the
  request index as `scan_ordinal`.

The sequence belongs to the database transaction, not an individual row. A
batch is ordered by ordinal and becomes visible atomically.

Assert `len(requests) <= capabilities.max_batch`, that ordinals start at zero,
and that identical replay returns before attempting a second order position.
The existing row keeps its original position.

### 3.4 Live-data migration and backfill

This migration requires a maintenance window; it is not rolling-upgrade safe.
Document and test this exact order:

1. Stop writers and matchers.
2. Acquire a table lock that prevents concurrent revision inserts.
3. Add the three nullable columns and the scan clock.
4. Put all pre-008 rows in closed `epoch=0`.
5. Assign deterministic best-effort positions by
   `(committed_at, stream, aggregate_id, revision, revision_id)`.
6. Set `scan_ordinal=0` for backfilled rows and a unique increasing
   `scan_sequence`.
7. Initialize the live epoch to `1` with an empty live sequence space.
8. Make columns non-null; create unique constraint/index.
9. Run Alembic's clean-schema check before restarting writers.

The backfill cannot reconstruct true historical commit order inside timestamp
ties. Say so in migration output and operations docs. Epoch separation ensures
all future rows nevertheless sort after all backfilled rows.

### 3.5 Restore/reset detection

Postgres transaction ids are cluster-local. `pg_dump` preserves the stored
numbers but a new cluster's xid counter may restart below them. Implement an
idempotent post-migration hook in `SqlAdmin.migrate()`:

1. lock the singleton scan clock row;
2. read `MAX(scan_sequence)` from the current epoch;
3. compare it with `pg_snapshot_xmin(pg_current_snapshot())`;
4. if the live value is below the stored maximum, increment `current_epoch`;
5. commit and report the epoch bump.

Run the check after every `schema upgrade`, even when Alembic has no new
revision to apply. The lock must be exclusive against the shared lock held by
every Eventic writer, so the old epoch cannot still have an in-flight writer
when it closes. A conservative false-positive epoch bump is harmless; a missed
reset is not.

Make `eventic schema upgrade` a mandatory pre-write deployment step after
`pg_dump`/restore, DR promotion, logical cutover, or a major-version migration.
Do not claim that an ordinary hot write can distinguish an xid reset from a
legitimate older concurrent transaction without either false positives or a
global exclusive lock. The supported restore procedure is quiesce → restore →
`schema upgrade` → writers. Never start post-restore writers before the epoch
reconciliation step.

Closed epochs need no xmin guard: all their rows predate the epoch transition
and are committed.

### Phase 3 tests

- Port both deterministic gap interleavings from `p02`.
- Port `p05` using two fresh Postgres clusters: dump, restore, detect reset,
  bump epoch, write, and scan all rows.
- Port `p07` to prove one batch scans in exact request order.
- Verify concurrent Postgres transactions never produce duplicate positions.
- Verify SQLite concurrent writers produce a gap-free total order.
- Verify an identical replay preserves its first position.
- Verify the migration on empty, populated snapshot, populated delta, and
  already-upgraded databases; test upgrade/downgrade where downgrade is
  supported.
- Verify old rows all precede new rows and document arbitrary historical tie
  order.

### Phase 3 exit gate

The naive BIGSERIAL and separate-sequence mechanisms fail the concurrency
regression, while the committed `(epoch, xid-or-counter, ordinal)` mechanism
passes on SQLite and live Postgres, including dump/restore.

---

## Phase 4 — Stable scanning as a separate capability

**Outcome:** a matcher can page through only stable rows without expanding the
core recording-store contract.

### 4.1 Protocols and capabilities

Extend `Capabilities` with:

```python
ordered_scan: bool = False
patterns: bool = False
```

Add a separate runtime-checkable protocol:

```python
class ProjectionStore(Protocol):
    @property
    def capabilities(self) -> Capabilities: ...

    def scan(
        self, *, after: ScanCursor | None, limit: int
    ) -> ScanPage: ...
```

Projection checkpoint/state methods arrive in Phase 7. Do not add `scan()` to
`Store` and do not require third-party recording stores to fake ordered scans.

`App.bind` rejects patterns unless the bound store supports both
`ordered_scan` and `patterns`. A selective-subscription-only app remains valid
on the old seven-method protocol.

### 4.2 Stable scan queries

SQLite query:

```text
WHERE position > :after
ORDER BY scan_epoch, scan_sequence, scan_ordinal
LIMIT :limit
```

SQLite needs no visibility watermark because `BEGIN IMMEDIATE` serializes
writes.

Postgres query:

```text
WHERE position > :after
  AND (
        scan_epoch < :live_epoch
        OR (
          scan_epoch = :live_epoch
          AND scan_sequence < pg_snapshot_xmin(pg_current_snapshot())
        )
      )
ORDER BY scan_epoch, scan_sequence, scan_ordinal
LIMIT :limit
```

Obtain the live epoch and snapshot xmin in the same short read transaction as
the page query. Return the effective stable horizon for status diagnostics.
Never use `committed_at` as a cursor or fall back to revision UUID ordering.

### 4.3 Hydration and `changed` cost

The scan result must contain logical `StoredRevision` documents and the same
top-level `changed` set used at plan time. Keep physical snapshot/delta details
inside the SQL store.

Implementation requirements from `p06`:

- hydrate one page at a time;
- for `snapshot/1`, fetch predecessor documents for the whole page in one
  additional set-based query;
- never issue one predecessor query per row on the snapshot path;
- for `delta/1`, reuse bounded-window decoding and measure its unavoidable
  folds explicitly;
- skip predecessor work for a pattern page only when static predicate analysis
  proves that no step uses a changed-dependent operator such as `Became`.

Expose query-count instrumentation in tests, not in the public API.

### 4.4 Liveness signal

`pg_snapshot_xmin` is cluster-wide. An unrelated write transaction—even in a
different database in the same cluster—can pin it and stall every Eventic
pattern. Record enough scan diagnostics to distinguish this from an idle log:

- last scan attempt time;
- last cursor advance time;
- current stable horizon;
- newest durable log position;
- whether rows exist beyond the stable horizon;
- age of the stall.

The matcher must not guess past the horizon. Status should say “visibility
horizon pinned” rather than silently appearing idle.

### Phase 4 tests

- Conformance property: once a cursor has been returned, no later page can
  contain a lower position.
- Concurrent creates and changes exercise the real xid-assignment timing from
  `p08`.
- An unrelated write transaction pins Postgres scanning; releasing it makes
  the rows visible without loss.
- A read-only transaction does not pin the horizon.
- Page sizes, empty pages, cursor encoding, epoch transitions, and batch
  boundaries are covered.
- Snapshot changed-key resolution stays at O(1) queries per page.
- Add a benchmark gate using `p06`'s workload. Record an explicit baseline for
  state-only, batched-changed snapshot, and delta paths; reject an accidental
  return to per-row snapshot predecessor queries.

### Phase 4 exit gate

Correctness concurrency tests and the throughput/query-count gate pass on both
stores. `Store` still has exactly seven methods.

---

## Phase 5 — Pattern, match, and declaration validation

**Outcome:** patterns are immutable application declarations with a stable
semantic fingerprint, and matches are ordinary canonical stream documents.

### 5.1 Add `patterns.py`

Define:

```python
@dataclass(frozen=True, slots=True)
class Step:
    stream: Stream[Any]
    when: Predicate = Always()


@dataclass(frozen=True, slots=True)
class Pattern:
    id: str
    version: int
    steps: tuple[Step, ...]
    correlate: Correlation
    within: timedelta
    emit: Stream[Match]
    cardinality_limit: int
```

Define frozen Pydantic documents:

```python
class RevisionRef(BaseModel):
    stream: str
    aggregate_id: UUID
    revision: int


class Match(BaseModel):
    pattern_id: str
    pattern_version: int
    correlation_key: str
    steps: tuple[RevisionRef, ...]
```

Persist references, never copies of matched state. Require at least two
references and preserve their stable scan order.

Add `match_id()` next to the other deterministic identity helpers. Hash a
canonical tuple of `(identity-format-version, pattern id, pattern version,
correlation key, terminal revision id)` with the fixed namespace. Do not build
the UUID input by ambiguous delimiter concatenation.

Strict-contiguous selection makes the steps tuple a pure function of the
terminal row. Add a defensive test that constructing the same id with a
different path causes the existing `RevisionConflict`; that failure is a
nondeterminism alarm, not something the matcher should swallow.

### 5.2 Pattern fingerprint

Canonical pattern JSON includes:

- a pattern-format version;
- pattern id and version;
- ordered step stream names and canonical predicates;
- canonical correlation expression;
- exact `within` duration in integer microseconds;
- emit stream name and schema version;
- selection semantics id, e.g. `strict-contiguous/1`.

It excludes operational tuning such as scan page size, poll interval, and
lease duration. Those values may change without changing match output.

### 5.3 Extend `App`

Add `patterns: Sequence[Pattern] = ()`, freeze it to a tuple, and aggregate
these declaration errors:

- duplicate `(pattern id, version)` or conflicting duplicate ids;
- id empty, version below one, fewer than two steps;
- non-positive `within` or cardinality limit;
- step/emit streams absent from `App.streams`;
- emit stream model is not exactly `Match`;
- a predicate or correlation field invalid for its stream/model;
- direct or transitive cycles in the pattern stream dependency graph;
- emit stream used as a source in a cycle;
- ordinary store bound to an app with patterns.

Allow acyclic pattern chaining: an emit stream from pattern A may feed pattern
B when the graph remains a DAG. Preserve app declaration order only for
inspection; scan order, not declaration order, determines matches.

### 5.4 Public exports and inspection

Export the documented declaration helpers from `eventic.__init__` without
importing SQLAlchemy. Extend `eventic inspect` with patterns, canonical
predicates/correlation, fingerprints, and required capabilities.

### Phase 5 tests

- Frozen/hashable declaration behavior.
- Aggregate validation reports multiple pattern defects at once.
- Fingerprint vectors are pinned as literals.
- Changing any semantic field changes the fingerprint; changing operational
  tuning does not.
- Callable predicates and callable correlation are rejected.
- Cycle detection covers self, two-node, and longer cycles.
- Match JSON contains references only and round-trips canonically.
- Match-id vectors and path-conflict alarm are pinned.
- Import-graph and package-surface tests remain green.

### Phase 5 exit gate

The full declarations can be imported and type-checked without constructing a
store, opening a connection, or importing SQLAlchemy.

---

## Phase 6 — Match delivery envelopes

**Outcome:** inline and outbox subscribers to a match stream receive a resolver
without adding ambient store state or a new delivery mechanism.

### 6.1 Define the envelope

Add `MatchEnvelope` to `envelopes.py`. It contains the ordinary
`Commit[Match, M]` plus a delivery-time resolver excluded from serialization:

```python
class MatchEnvelope[M: BaseModel]:
    commit: Commit[Match, M]

    @property
    def match(self) -> Match: ...

    def resolve(self) -> tuple[Revision[Any, Any], ...]: ...
```

The concrete resolver holds the current `App` and `Store`, resolves each
`RevisionRef` through `Store.revision`, chooses the declared stream by name,
and hydrates it. Return a positionally stable but weakly typed tuple. Do not
pretend variadic generics can preserve each step's distinct state type; `p01`
proved that they cannot.

The resolver is never persisted, included in a digest, installed globally, or
stored in a ContextVar. A dangling reference is a `DeliveryError` naming the
reference and pattern.

### 6.2 Dispatch integration

Build one helper that chooses the delivery envelope:

```python
delivery_envelope(app, store, stream, commit) -> Commit | MatchEnvelope
```

Use it in both:

- `dispatch_inline`, after the commit returns;
- `Worker._reconstruct`, after hydrating an outbox revision.

Identify a match stream from the validated app's pattern emit targets, not by
checking an arbitrary payload shape at runtime.

Update handler validation:

- ordinary streams accept `Commit[T, M]`;
- pattern emit streams accept `MatchEnvelope[M]`;
- untyped one-argument handlers remain accepted under the current best-effort
  policy;
- a typed mismatch is a declaration error.

No new queue, retry, lease, settlement, or dead-letter code is permitted. The
worker continues to operate on existing outbox intents.

### Phase 6 tests

- Port `p01` into unit and typing tests.
- Inline and outbox handlers receive equivalent `MatchEnvelope` values.
- `resolve()` returns revisions in exact step order across multiple streams.
- A missing stream or revision fails with a clear `DeliveryError`.
- The envelope/resolver never appears in serialized `Match` bytes.
- Existing ordinary subscriptions still receive `Commit`.
- Retry and dead-letter behavior is unchanged when resolution or the handler
  fails.

### Phase 6 exit gate

A persisted match can be delivered and resolved through both existing delivery
strategies with no ambient store and no new delivery machinery.

---

## Phase 7 — Projection persistence, fencing, and operations wire types

**Outcome:** one matcher owns a pattern version at a time, and stale owners
cannot corrupt state or checkpoints.

### 7.1 Tables

Add these tables in the Phase 3 migration if phases land together, or in the
next migration if Phase 3 has already shipped.

`eventic_pattern_schema`:

```text
pattern_id          TEXT
pattern_version     INTEGER
fingerprint         CHAR(64)
first_seen          TIMESTAMPTZ
PRIMARY KEY(pattern_id, pattern_version)
```

`eventic_projection`:

```text
pattern_id          TEXT
pattern_version     INTEGER
cursor_epoch        INTEGER NULL
cursor_sequence     xid8/INTEGER NULL
cursor_ordinal      INTEGER NULL
watermark_at        TIMESTAMPTZ NULL
updated_at          TIMESTAMPTZ NOT NULL
last_advanced_at    TIMESTAMPTZ NULL
last_error          TEXT NULL
leased_until        TIMESTAMPTZ NULL
lease_owner         UUID NULL
lease_epoch         INTEGER NOT NULL DEFAULT 0
PRIMARY KEY(pattern_id, pattern_version)
```

`eventic_match_state`:

```text
pattern_id          TEXT
pattern_version     INTEGER
correlation_key     TEXT
step_index          INTEGER NOT NULL
matched_refs        JSON/JSONB NOT NULL
opened_at           TIMESTAMPTZ NOT NULL
deadline            TIMESTAMPTZ NOT NULL
updated_at          TIMESTAMPTZ NOT NULL
PRIMARY KEY(pattern_id, pattern_version, correlation_key)
FOREIGN KEY(pattern_id, pattern_version) -> eventic_projection ON DELETE CASCADE
INDEX(pattern_id, pattern_version, deadline)
```

One row per key is the physical enforcement of no overlap.

### 7.2 Projection wire values

Add frozen values for:

- `ProjectionLease(pattern_id, version, owner, lease_epoch, cursor,
  leased_until)`;
- `PartialMatch`;
- `StateUpsert` and `StateDelete`;
- `ProjectionApply(expected_cursor, next_cursor, watermark, mutations)`;
- `ProjectionStatus` and `ProjectionReport`.

Do not pass SQLAlchemy rows or connections through the protocol.

### 7.3 Extend `ProjectionStore`

Use a narrow transaction-oriented surface rather than exposing table CRUD:

```python
def acquire_projection(..., lease: timedelta) -> ProjectionLease | None: ...
def renew_projection(lease: ProjectionLease, *, duration: timedelta) -> ProjectionLease: ...
def load_match_state(lease: ProjectionLease) -> tuple[PartialMatch, ...]: ...
def apply_projection(lease: ProjectionLease, change: ProjectionApply) -> bool: ...
def release_projection(lease: ProjectionLease) -> None: ...
```

Admin-only reset/status methods belong on a separate `ProjectionAdmin`
protocol or the existing `StoreAdmin` extension.

### 7.4 Fencing rules

Lease acquisition is one conditional update/insert that:

- succeeds only when unleased, expired, or already owned by the caller;
- increments `lease_epoch` whenever ownership is newly acquired;
- returns the new epoch and cursor.

Every renewal, error update, state mutation, and cursor advance includes both
`lease_owner` and `lease_epoch` in its predicate. `apply_projection` also
compares the stored cursor with `expected_cursor`.

State mutations and cursor advancement occur in **one database transaction**.
If the fenced cursor update affects zero rows, roll back all match-state
changes and return `False`; the runtime stops immediately. Never let a stale
worker move a cursor backward or forward.

Use the database clock for lease timestamps. Wall time is allowed for process
ownership; it must never determine which matches exist.

### 7.5 Cardinality enforcement

Before applying state upserts, count current plus prospective active keys. If
it exceeds `Pattern.cardinality_limit`, atomically record a projection error,
do not advance the cursor, and stop. `projection status` must expose current
and configured cardinality.

This is a circuit breaker, not an eviction policy. Evicting arbitrary keys
would silently change semantics.

### Phase 7 tests

- Port the naive and fenced lease schedules from `p11`.
- A stale owner cannot renew, write an error, mutate state, or advance cursor.
- State and cursor commit together or not at all under injected failures.
- Expected-cursor CAS rejects a second writer even with a forged owner.
- Lease timestamps come from the database.
- Cardinality overflow stops without eviction or cursor movement.
- Migrations and Alembic clean checks pass on both dialects.

### Phase 7 exit gate

The cursor rollback from 500 to 100 in `p11` is impossible, and injected
transaction failures leave state and cursor at the same logical point.

---

## Phase 8 — The matcher runtime

**Outcome:** the matcher deterministically derives positive matches, survives
crashes, and keeps partial state bounded during idle periods.

### 8.1 Pure transition core

Implement the matching state machine as a pure function before adding the
process loop:

```python
transition(
    pattern: Pattern,
    partials: Mapping[str, PartialMatch],
    rows: Sequence[ScannedRevision],
) -> TransitionPlan
```

`TransitionPlan` contains ordered match emissions, final state upserts/deletes,
the expected and next cursor, and the maximum event-time watermark reached.

For each relevant row:

1. build `PredicateView` from `ScannedRevision`;
2. evaluate the declarative correlation expression;
3. expire that key when `row.committed_at > deadline`;
4. evaluate only the next expected step;
5. on mismatch, clear the partial and retry the same row against step 0;
6. on step-0 match, open with `deadline = committed_at + within`;
7. on terminal match, create `Match`, calculate deterministic id, queue an
   emission, and delete the partial;
8. never reuse the terminal row.

Preserve scan order in both emitted matches and stored references. The pure
function accepts no store, runtime, process clock, randomness, or logger.

### 8.2 Runtime cycle and crash ordering

For one leased pattern version:

1. scan a stable page after the leased cursor;
2. load/reuse partial state and calculate a `TransitionPlan` in memory;
3. renew the lease when needed before doing slow work;
4. append **all planned `Match` documents first** through the ordinary
   `Runtime[pattern.emit].create(...)` path;
5. only after every emission succeeds, call fenced `apply_projection` to
   atomically mutate partial state and advance the cursor;
6. if apply loses the fence, discard memory state and stop;
7. if any operation fails, record a fenced diagnostic when still owner, then
   stop without advancing.

The cycle intentionally has at most two durable stages: one ordinary Eventic
transaction per emission, followed by one atomic projection-state/cursor
transaction. A crash after an emission but before apply causes a rescan and
re-emission. The existing replay path absorbs duplicate log/outbox writes.
Inline match handlers may run again and therefore remain at-least-once.

Never mutate match state before the matching output is durable. That is the
silent-loss side of the crash window demonstrated by `p11`.

### 8.3 Matcher process

Add a `Matcher` analogous in lifecycle—not internals—to `Worker`:

- accepts `App`, `Store & ProjectionStore`, optional pattern selector, lease,
  page size, and poll interval;
- `run_once()` processes at most one page per selected pattern and returns a
  structured report;
- `run_forever()` uses a stop event and never installs signal handlers;
- the CLI owns SIGTERM/SIGINT and calls `stop()`;
- one process may iterate multiple patterns, but ownership remains one lease
  per pattern version;
- no module-level registry or mutable global is introduced.

### 8.4 Heartbeats and idle-state cleanup

Heartbeats ship now, not with future negation.

When a pattern is caught up and idle:

1. take an exclusive lock on `eventic_scan_clock`; this waits for all earlier
   Eventic write transactions and blocks later ones briefly;
2. while holding it, recheck that no durable revision position exists beyond
   the projection cursor—if one does, release and scan instead;
3. read `clock_timestamp()` only after the barrier is held; do not use
   transaction-start `now()`;
4. advance `eventic_projection.watermark_at` under the current projection
   fence;
5. delete partials whose deadline is below that watermark in the same
   transaction;
6. commit the watermark/state cleanup before releasing the clock barrier;
7. do not emit a match from heartbeat expiry.

For the positive-only release, heartbeats affect storage cleanup and liveness,
not the output match set. Rebuild resets cursor, state, and watermark, then
re-derives outputs solely from ordered rows. Future absence/negation semantics
will require a separate durable-watermark-history design; do not smuggle them
into this release.

Never apply a watermark merely because the stable scan returned an empty page.
That page may be stopped by a cluster-wide xmin pin while committed Eventic rows
exist beyond the horizon. The clock barrier plus the unfiltered durable-position
recheck is the proof that no earlier or in-flight Eventic row can later complete
an expired partial.

### 8.5 Match-state memory behavior

Load state once per acquired lease and update the in-memory map only after
`apply_projection` commits. After a fence loss, throw it away. Page processing
must not issue one state query per row.

### Phase 8 tests

- Table-driven pure tests for every strict-contiguity rule.
- Cross-stream patterns preserve user batch order.
- Three positive strikes emit one match, with no overlap.
- Window boundary is inclusive; one microsecond beyond resets/restarts.
- Replay from every possible checkpoint yields the identical path and payload.
- Port `p04` and `p07`: same emission is absorbed; a different terminal creates
  a new match; a different path under one id is a hard alarm.
- Port both crash orders from `p11`; only emit-first survives.
- Crash after each emitted match, after the last emit, before apply, during
  apply, and after apply.
- Lease expiry/takeover while a matcher is stalled cannot corrupt state.
- Heartbeat clears idle positive partials without emitting.
- Rebuild at different wall-clock dates produces byte-identical match docs.
- Cardinality tests from `p10` stay bounded by active keys and stop at budget.
- Worker/outbox tests prove no delivery machinery changed.

### Phase 8 exit gate

The three-strike scenario emits one durable `Match`; restarting at every
injected crash point converges to the same log, intents, state, and cursor.

---

## Phase 9 — Operations, fingerprint ledger, and rebuild

**Outcome:** operators can run, inspect, diagnose, and rebuild projections
without editing tables by hand.

### 9.1 Matcher CLI

Add:

```console
eventic --app module:app --url ... matcher [--pattern ID] [--once]
```

Mirror worker process ownership: commands install signal handlers; the library
does not. Print structured counts for scanned rows, matches emitted/replayed,
state upserts/deletes, cursor advances, lease misses, and failures.

### 9.2 Projection status

Add:

```console
eventic ... projection status [--pattern ID]
```

Report at minimum:

- pattern id/version and declared/stored fingerprint;
- cursor and newest stable/durable positions;
- lag in rows when computable;
- last scan and cursor-advance timestamps;
- stable horizon and pinned-horizon age;
- lease owner/epoch/expiry;
- watermark;
- active state rows and cardinality budget;
- oldest/newest deadline;
- last error.

Use truthful exit codes: drift/config is usage/drift, operationally failed
projection is failure, healthy/idle is success.

### 9.3 Pattern fingerprint ledger

The matcher records a pattern fingerprint baseline on first ownership in the
same spirit as stream fingerprints on first write. `schema check` remains
read-only:

- no stored baseline: report “no baseline recorded”;
- same fingerprint: `ok`;
- different fingerprint for same id/version: drift and refuse matcher start;
- new version: independent baseline and output stream entries.

Never update the stored fingerprint merely because the declaration changed.
Require a version bump.

Extend `SchemaReport` or add `PatternSchemaReport` without breaking existing
stream output consumers. Update `eventic inspect` and schema-check tests.

### 9.4 Rebuild

Add:

```console
eventic ... projection rebuild --pattern ID [--version N] [--chunk N]
```

Rebuild procedure:

1. refuse while a non-expired matcher lease exists unless the operator passes
   an explicit, documented takeover flag;
2. verify the declared fingerprint matches the stored version;
3. acquire an admin fencing epoch;
4. transactionally clear partial state and reset cursor/watermark/error;
5. replay synchronously through the same matcher transition and emission code;
6. keep emitted matches in the append-only log;
7. rely on deterministic ids to absorb identical outputs;
8. report new, replayed, conflicting, and failed emissions plus final cursor.

Do not delete old match documents during rebuild. Pattern output is recorded
truth. A changed behavior gets a new pattern version and therefore new match
ids alongside old output.

For the initial positive-only matcher, rebuild need not reproduce historical
cleanup heartbeat timing because expiry never emits. It must reproduce every
`Match` document byte-for-byte.

### 9.5 Verification

Extend `eventic verify` or add `projection verify` to check:

- each `Match.steps` reference exists;
- referenced rows are in increasing scan order;
- terminal row agrees with deterministic match id;
- pattern id/version has a known fingerprint;
- projection cursor refers to an existing or valid horizon position;
- partial states obey index/reference/deadline constraints;
- no state row exceeds its pattern's cardinality limit.

Verification is read-only and never “repairs” drift.

### Phase 9 tests

- CLI parsing, output, and exit codes for matcher/status/rebuild/verify.
- Fingerprint baseline, missing baseline, drift, and version bump.
- Rebuild from cursor zero yields byte-identical match payloads/digests.
- Rebuild while leased refuses safely; stale takeover is fenced.
- Rebuild does not duplicate outbox intents or delete recorded matches.
- Status distinguishes idle, lagging, xmin-pinned, lease-held, drifted,
  cardinality-stopped, and failed projections.
- Verify detects dangling/out-of-order references and corrupt partial state.

### Phase 9 exit gate

A clean database can migrate, run the matcher, display status, rebuild, and
verify through public CLI commands. Every command has deterministic output and
truthful exit status.

---

## Phase 10 — Conformance, documentation, and release gate

**Outcome:** the feature is a supported library contract rather than a SQL-only
implementation detail.

### 10.1 Conformance suite

Add projection conformance scenarios beside the existing store scenarios, but
gate them on capabilities:

- ordered scan and concurrent visibility;
- exact batch order;
- epoch transition;
- snapshot/delta semantic equivalence;
- lease fencing and atomic apply;
- matcher crash convergence;
- deterministic rebuild;
- state cardinality enforcement.

Export the `ProjectionStore` conformance contract for third-party store authors.
Do not make pattern support mandatory for stores that advertise neither
capability.

### 10.2 Documentation

Update:

- `README.md`: both response tiers, full declaration example, delivery
  guarantees, weak match typing, and resolver usage;
- `docs/INVARIANTS.md`: corrected I12–I17;
- `docs/STORE_AUTHORS.md`: optional `ProjectionStore`, capability semantics,
  cursor stability property, and conformance entrypoint;
- `docs/OPERATIONS.md`: matcher lifecycle, lease/fence behavior, xmin stall
  diagnosis, cardinality budgets, rebuild, and restore epoch reconciliation;
- `docs/EVOLUTION.md`: pattern versions/fingerprints and why behavior changes
  require a bump;
- `docs/DELIVERY.md`: match emissions use ordinary delivery; outbox duplicate
  intents are suppressed, inline handlers remain at-least-once;
- `docs/BENCHMARKS.md`: `p06`-style throughput numbers and the cost of
  changed-dependent predicates;
- `docs/ASYNC_READINESS.md`: new protocols stay one-request/one-value and own no
  SQLAlchemy session above the SQL layer.

Explicitly document these limits:

- Postgres visibility can be stalled by any open write transaction in the same
  cluster, including another database.
- The matcher is one writer per pattern version and is not sub-millisecond.
- Match resolution is positionally stable but weakly typed.
- Existing-row backfill is approximate inside timestamp ties.
- Correlation state can approach `arrival rate × window`; the budget is a hard
  stop, not eviction.
- Only positive strict-contiguous patterns ship; no negation or overlap.

### 10.3 Definition-of-done assertions

Extend architecture/definition-of-done tests to mechanically require:

- public exports exist and import without SQLAlchemy;
- core `Store` remains seven methods;
- `ProjectionStore` is optional and capability-gated;
- tables, migration, and metadata stay synchronized;
- no module-level mutable registry or installed signal handler;
- new wire/declaration values are frozen;
- package builds include the new modules and typing marker;
- README example executes against SQLite;
- all warnings are errors.

### 10.4 Final evidence matrix

Run and record:

1. full SQLite suite;
2. full live-Postgres suite;
3. Postgres dump/restore test against a second cluster;
4. deterministic concurrency scenarios from `p02`, `p07`, `p08`, and `p11`;
5. snapshot and delta matcher equivalence;
6. the scan throughput benchmark with query counts;
7. wheel build/install/import and README integration test;
8. migration from a populated 1.1 database and a clean install;
9. `devenv shell -- repoman doctor`.

Do not release if any Postgres-only scenario is skipped. Preserve the commands,
versions, row counts, timings, and results in a dated outcome document.

### Phase 10 exit gate

All supported stores are green; the restore, concurrency, crash, rebuild, and
throughput gates have fresh evidence; documentation states the limits above;
and RepoMan reports a healthy repository before the final GitMan save/push.

---

## File-by-file change checklist

This checklist is a cross-check, not a substitute for phase order.

| File | Required change |
|---|---|
| `src/eventic/predicates.py` | PredicateView, predicate algebra, correlation algebra, canonical forms/fingerprints. |
| `src/eventic/patterns.py` | Step, Pattern, RevisionRef, Match, pattern fingerprint, deterministic match id. |
| `src/eventic/subscription.py` | Add value-based `when`, preserving `Always` default. |
| `src/eventic/planning.py` | Compute state/meta trees and changed keys before intents; evaluate predicates. |
| `src/eventic/runtime.py` | Reuse planned changed semantics; construct match delivery envelope after commit. |
| `src/eventic/dispatch.py` | Predicate filter and ordinary-vs-match envelope construction. |
| `src/eventic/envelopes.py` | Add non-persisted resolver-backed MatchEnvelope. |
| `src/eventic/app.py` | Patterns field; aggregate validation; DAG/capability/handler checks. |
| `src/eventic/projection.py` | Pure transition function and Matcher lifecycle. |
| `src/eventic/protocols.py` | Capabilities, separate ProjectionStore/Admin, reports. Keep Store unchanged. |
| `src/eventic/wire.py` | Scan cursor/page, lease, partial state, mutations, status/report values. |
| `src/eventic/worker.py` | Construct MatchEnvelope on outbox reconstruction; no new retry machinery. |
| `src/eventic/ids.py` | Canonical deterministic match-id helper and pinned vectors. |
| `src/eventic/sql/tables.py` | Scan clock/order columns, pattern ledger, projection, match-state tables/indexes. |
| `src/eventic/sql/dialect.py` | Native xid8 support and dialect-specific stable-scan/lease expressions. |
| `src/eventic/sql/statements.py` | Stable scan, predecessor batch, lease acquire/renew, fenced atomic apply, status/reset. |
| `src/eventic/sql/store.py` | Position assignment, ordered scan, changed hydration, reset refusal. |
| `src/eventic/sql/projection.py` | Optional extraction of SQL projection persistence from store.py. |
| `src/eventic/sql/admin.py` | Epoch reconciliation, fingerprint ledger, status/rebuild/verify. |
| `src/eventic/sql/migrations/versions/0002_derived_projection.py` | Live-data-safe backfill and all schema additions. |
| `src/eventic/cli/main.py` | `matcher` and `projection status/rebuild/verify` parsing. |
| `src/eventic/cli/commands.py` | Process lifecycle, reports, exit codes, inspect output. |
| `src/eventic/__init__.py` | Pure public declarations/helpers/envelopes only. |
| `src/eventic/testing/conformance/` | Optional projection-store and matcher scenarios. |
| `tests/unit/` | Predicate, pattern, state-machine, planning, app, envelope tests. |
| `tests/conformance/` | Stores, ordering, migration, fencing, worker/matcher/admin/CLI tests. |
| `tests/property/` | Predicate canonicalization, scan paging, replay/rebuild determinism. |
| `tests/typing/` | Public declaration and weak MatchEnvelope resolve boundary. |
| `tests/architecture/` | Imports, protocol size, no global state, migration/metadata sync, packaging. |

---

## Failure modes that must remain visible

The implementation is incomplete if it hides any of these:

| Failure | Required behavior |
|---|---|
| Postgres xmin is pinned | Stop at the stable horizon; status names the stall and its age. |
| Cluster xid reset after restore | The supported quiesced restore procedure runs `schema upgrade`, which detects the reset and bumps the epoch before writers start. |
| Pattern fingerprint changes without version bump | Schema drift; matcher refuses ownership. |
| Correlation key is missing/invalid | Record error and leave cursor unchanged. |
| Cardinality budget is exceeded | Stop projection; do not evict or skip keys. |
| Matcher loses its lease | Fenced apply fails, all pending state changes roll back, matcher stops. |
| Matcher crashes after emit | Rescan and re-emit; log/outbox replay is absorbed, inline may run again. |
| Match id resolves to different payload | Raise `RevisionConflict` as a nondeterminism alarm. |
| Referenced revision is missing | Match delivery/verify fails loudly; no fabricated state. |
| Backfilled timestamps tie | Deterministic approximate order, documented as historical uncertainty. |

Never recover from these by advancing the cursor, deleting truth, or silently
dropping a row.

---

## Explicitly deferred work

Do not expand the first implementation to include:

- negation or “A without B” matches;
- expiry-triggered emissions;
- durable historical watermarks required by negation rebuilds;
- out-of-order user event time;
- overlapping/non-contiguous/NFA matching;
- key sharding or multiple writers per pattern version;
- variadically typed match steps;
- arbitrary callable predicates/correlation;
- automatic pruning of recorded match streams.

Each changes either determinism, identity, state growth, or ownership and needs a
separate concept plus adversarial probes.

---

## Recommended save sequence

Use one independently green GitMan change per phase:

1. predicate/correlation values;
2. selective subscriptions;
3. order schema, migration, and write assignment;
4. stable scan and performance gate;
5. pattern/match declarations;
6. match delivery envelope;
7. projection persistence and fencing;
8. matcher and heartbeat;
9. operations and rebuild;
10. conformance, documentation, and release evidence.

Verify before every save. Publish each completed lane promptly, and do not fold
a phase whose exit gate is red.
