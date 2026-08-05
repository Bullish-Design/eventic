# Eventic 1.0 — Implementation Guide

**Companion to** `CONCEPT.md` (the idea, the invariants, the definition of done) and
`ARCHITECTURE.md` (the module graph, the types, the store contract, the async rules).

Seventeen phases. Each phase states an **outcome**, ordered **steps**, the **tests it
must produce**, and an **exit gate** that is a command with a pass/fail result — never
a judgement. Do not start a phase until the previous gate is green.

Invariants `I1`–`I10` are in `CONCEPT.md` §4. Async rules `R1`–`R10` are in
`ARCHITECTURE.md` §9.

---

## 0. How to use this guide

- **The order is load-bearing.** Phases 1–5 build the entire correctness core with no
  database at all. If those are right, the rest is plumbing; if they are wrong, no
  amount of plumbing saves it. Resist the urge to get a row into a table early.
- **A phase is done when its gate passes**, not when it feels finished.
- **Never weaken an assertion to make a test pass.** If a test is wrong, delete it and
  say why in the commit message.
- **No test is named after a review finding.** Name tests after behavior. The 0.3 suite
  was a diary of past bugs and missed every present one.

### 0.1 Working discipline

1. Write the failing behavioral test before the implementation, every time.
2. Prefer pure functions at every boundary: validation, canonicalization, planning,
   hydration, retry, upcasting.
3. Never `model_construct()` with persisted data. Never `model_copy(update=...)` for a
   validated change.
4. Never pass a SQLAlchemy `Session`, `Connection`, or `Engine` across a protocol
   boundary (R5).
5. Never introduce module-level mutable state (R9). If you need one, the design is
   wrong somewhere upstream.
6. Do not mock the database in a store test. SQLite is fast enough to be real.
7. Treat every warning as an error.
8. Do not optimize before Phase 15.

### 0.2 The per-phase command

```console
uv run ruff check .
uv run ruff format --check .
uv run basedpyright
uv run pytest -W error
```

Early phases scope the last two to what exists. From Phase 9 onward the full command
is mandatory and is the CI job.

---

## Phase 0 — Reset the repository

**Outcome:** an empty, correctly packaged project with quality gates green on zero
code, and the 0.x tree gone.

### Steps

1. Branch `v1`. Delete `src/eventic/**` and `src/tests/**` wholesale. There is no live
   data (`CONCEPT.md` preamble), no compatibility surface, and nothing to port.
2. Move tests out of `src/`: the layout is `src/eventic/` and top-level `tests/`.
3. Rewrite `pyproject.toml` per `ARCHITECTURE.md` §10. Drop `python-dotenv`. Move
   `alembic` to the `migrate` extra, `psycopg` to `postgres`. Single version source
   read from package metadata.
4. Add `src/eventic/py.typed`.
5. Configure `ruff` (line length 88, full `E`/`F`/`I`/`UP`/`B`/`SIM` sets) and
   `basedpyright` with a **deliberate, checked-in baseline** — strictness you intend to
   hold, not defaults you will silence later.
6. Add `.github/workflows/ci.yml`: matrix over Python 3.13 and a live Postgres service,
   running the §0.2 command plus `uv build`. The README badge must point at a workflow
   that exists.
7. Add sdist excludes for `.scratch/`, `probes/`, `uv.lock`, and local config.
8. Archive `.scratch/projects/00{1,2,3,4}` as read-only history. They are the evidence
   base; they are not shipped.

### Tests

- `tests/architecture/test_packaging.py`: the built wheel contains `py.typed`, no
  `.scratch`, and no test package.

### Exit gate

```console
uv build && uv run pytest -W error && uv run ruff check . && uv run basedpyright
```

Green, with the repository containing no implementation.

---

## Phase 1 — Leaves: errors, ids, canonical JSON

**Outcome:** `errors.py`, `ids.py`, `jsonx.py`. No eventic imports, no I/O.

### Steps

1. `errors.py` — the full tree from `ARCHITECTURE.md` §8, verbatim, with structured
   attributes on every class. `NotFound` does **not** subclass `KeyError`.
2. `ids.py` —
   - `StreamName`: an `Annotated[str, ...]` validated against `^[a-z0-9][a-z0-9_.-]{0,63}$`.
   - `AggregateKey(stream, aggregate_id)` — a frozen slotted dataclass, hashable.
   - `revision_id(stream, aggregate_id, revision) -> UUID` = `uuid5(NS, f"{stream}:{id}:{revision}")`.
     One function. Never a seam (003/F9 killed the identity seam for exactly this reason).
   - `NS`: a fixed, checked-in namespace UUID with a comment saying it must never change.
3. `jsonx.py` —
   - `JsonValue` recursive type alias.
   - `canonical_bytes(tree) -> bytes`: `json.dumps(sort_keys=True,
     separators=(",",":"), ensure_ascii=False, allow_nan=False).encode("utf-8")`.
   - `digest(payload: bytes) -> str`: sha256 hex.

### Tests

- `revision_id` is stable across processes and platforms; pin three known vectors as
  literals so a future refactor that changes the formula fails loudly.
- `canonical_bytes` is order-independent for dict inputs, rejects `NaN`/`Infinity`,
  and produces identical bytes for semantically identical trees built in different
  orders.
- Every error class carries its documented attributes and renders without a payload.

### Exit gate

100% line and branch coverage on all three modules. They are leaves; there is no
excuse.

---

## Phase 2 — Canonicalization and the type zoo

**Outcome:** `canonical.py` plus the corpus that proves it. This is the highest-risk
pure module in the library; it gets a full phase.

### Steps

1. Implement the static computed-field exclude-map builder: walk the annotated model
   graph from a root `type[BaseModel]`, collecting `model_computed_fields` at every
   depth into Pydantic's nested-`exclude` form, using `__all__` for sequences and
   mappings. Cache per type. Handle recursion with a seen-set.
2. Implement `canonicalize(adapter, exclude_map, value) -> bytes` per
   `ARCHITECTURE.md` §3.1 steps 2–3.
3. Implement `verify(adapter, exclude_map, payload) -> None`: `validate_json`,
   re-canonicalize, require byte equality, else `UndecodableRevision` naming the
   diverging JSON pointer.
4. Build `testing/factories.py`: the type zoo from `ARCHITECTURE.md` §3.2 — every
   supported Pydantic construct, with computed fields at depths 0, 1, and 2, and inside
   sequences and mappings.
5. Decide `SecretStr` explicitly: either it round-trips losslessly or `Stream`
   construction rejects a model containing one. **It must never serialize as
   `"**********"` into an append-only log.** Write the test that would catch that.

### Tests

- Property (Hypothesis) over the zoo:
  `canonicalize(validate(canonicalize(x))) == canonicalize(x)`.
- A model with a computed field at each depth: the computed key appears in
  `model_dump()` and is absent from the canonical bytes.
- A model whose union branch defeats the static walk: `verify` raises at write time
  rather than producing an undecodable payload. This is the safety-net test; it must
  exist and must fail if step 3 is removed.
- Reordering fields in a model's source does not change the canonical bytes.
- Aliases, field serializers, and model serializers do not change durable semantics
  without changing the digest.

### Exit gate

The property test passes with `max_examples=1000` over the whole zoo, and removing
`verify` makes at least one test fail.

---

## Phase 3 — Declarations: Stream, Meta, envelopes

**Outcome:** `stream.py`, `meta.py`, `envelopes.py`, `evolution.py`. Still no I/O.

### Steps

1. `Stream[T]` per `ARCHITECTURE.md` §2.1: frozen, hashable by name, caching
   `TypeAdapter`, exclude map, and model fingerprint (sha256 of the model's JSON
   schema, key-sorted).
2. `Meta[M]` and `NoMeta`. Same machinery, applied to metadata. One mechanism, twice.
3. `evolution.py`: `Upcaster` protocol (`from_version`, `to_version`, `__call__(JsonObject) -> JsonObject`),
   chain validation, and `upcast(tree, from_version, to_version)`. Upcasters receive
   JSON, are deterministic, and get no store, clock, or context.
4. `envelopes.py`: `Revision[T, M]`, `Commit[T, M]`, `Page[X]`, all generic Pydantic
   models with `frozen=True`.

### Tests

- `Stream("todos", …)` twice with different models raises `DuplicateId` when both are
  installed in one `App` (deferred to Phase 4 for the check; here, assert names are the
  identity).
- Chain validation: `schema_version=3` with upcasters `{1: …}` raises
  `IncompleteUpcasterChain` naming the missing `2 → 3` transition.
- `Revision[Todo]` produces a usable JSON schema (`model_json_schema()`) with `state`
  fully expanded — the property that makes FastAPI integration free.
- `Revision` and `Commit` are frozen; assignment raises.
- A `RootModel` or non-`BaseModel` stream type raises `ConfigError`.

### Exit gate

`tests/typing/` contains a fixture asserting `Stream(Todo, …)` narrows to
`Stream[Todo]` and that `Revision[Todo].state` is `Todo` under `basedpyright`.

---

## Phase 4 — `App` and declaration-time validation

**Outcome:** `app.py`, `subscription.py`. Every declaration error is caught at
construction, and all of them are reported at once.

### Steps

1. `Subscription`, `Inline`, `Outbox`, `Backoff` per `ARCHITECTURE.md` §2.1.
2. `App` as a frozen Pydantic model. Its validator runs the whole table in
   `ARCHITECTURE.md` §2.1, **collects** every failure, and raises one `ConfigError`
   whose message lists them, one per line, each naming the offending id.
3. `App.bind(store)` — capability check (`Outbox` subscription against
   `capabilities.outbox=False` → `CapabilityUnsupported`) and return a `Runtime`.
   Nothing else. `bind` opens no connection.
4. Reject coroutine handlers with the forward-compatible message *"async handlers are
   not supported in 1.0"* (R6).

### Tests

- Duplicate stream names, duplicate subscription ids, a subscription referencing an
  uninstalled stream, and an async handler — declared together — produce **one** error
  listing all four.
- Constructing an `App` performs no I/O: run it with the network and filesystem
  patched to raise (I4).
- `App` is hashable and deep-copyable; two identical declarations compare equal.

### Exit gate

`tests/architecture/test_no_global_state.py` passes: importing every eventic module
defines no module-level mutable binding (R9).

---

## Phase 5 — The pure commit core

**Outcome:** `wire.py`, `planning.py`, `hydration.py`, `retry.py`. The entire
correctness core, with zero I/O and zero database. **This is the phase that decides
whether 1.0 works.**

### Steps

1. `wire.py`: `CommitRequest`, `CommitResult`, `StoredRevision`, `IntentRequest`,
   `ClaimedIntent`, `Settlement`, `Disposition` — frozen slotted dataclasses.
2. `planning.py`:
   - `plan_create(app, stream, state, id, meta) -> CommitRequest`
   - `plan_change(app, stream, base, fields, meta) -> CommitRequest`
   - `plan_replace(app, stream, base, state, meta) -> CommitRequest`
   - `changed_keys(before: JsonObject | None, after: JsonObject) -> frozenset[str]`
   - `intents_for(app, stream, kind, revision_id) -> tuple[IntentRequest, ...]`

   `plan_change` builds the new state as
   `adapter.validate_python(base.state.model_dump(mode="python") | fields)` — never
   `model_copy(update=...)`. `expected_revision` is `None` for create and
   `base.revision` for change/replace.
3. `hydration.py`: `hydrate(stream, meta, StoredRevision) -> Revision[T, M]` —
   upcast the JSON tree if `schema_version` is behind, validate, and construct the
   envelope. Encoding-agnostic by construction: it receives a logical document.
4. `retry.py`: `disposition(attempts, backoff, error) -> Disposition` — pure, no clock
   read; the current time is an argument.

### Tests

- A stale `base` (revision behind the head) yields a request with the *stale*
  `expected_revision` — planning does not repair it; the store rejects it. Prove the
  request is built faithfully.
- `changed_keys` is computed from canonical documents, is JSON-native, and never
  contains a caller kwarg name that Pydantic coerced away.
- Round trip: `hydrate(stream, meta, stored_from(plan_create(...)))` reproduces the
  input state exactly, by digest.
- `disposition` is a pure function: same inputs, same output, no clock, no randomness.
- Every function in these four modules is importable and callable with no store, no
  connection, and no `App.bind`.

### Exit gate

Branch coverage on `planning.py`, `hydration.py`, and `retry.py` is 100%, and
`tests/architecture/test_purity.py` asserts none of the four modules imports
`sqlalchemy`, `os`, `time`, `random`, or `datetime.now` (R3).

---

## Phase 6 — The store contract and its conformance suite

**Outcome:** `protocols.py` and `testing/conformance/store.py` — **written before any
store exists**. The suite is the specification; the backends are attempts to satisfy it.

### Steps

1. `protocols.py`: `Store` (seven methods), `StoreAdmin`, `Capabilities`, per
   `ARCHITECTURE.md` §4.2. No SQLAlchemy import, no `Session` parameter, no `Iterator`
   return (R1, R2, R5).
2. `testing/conformance/store.py`: scenarios as **data** — a list of frozen scenario
   objects, each with a name, a required capability set, a sequence of operations, and
   expected outcomes. No `assert` inside the data.
3. `testing/runner.py`: a sync runner that executes a scenario against a `Store`
   factory. The future async runner is a second file (R8).
4. Author the scenarios. Minimum coverage:

| Group | Scenarios |
|---|---|
| CAS | create on empty; create when exists → conflict; change with correct/stale/ahead/negative expected revision; change on a nonexistent aggregate |
| Replay | byte-identical replay → `replayed=True`, one row; same key, different digest → conflict; same key, different meta → conflict; same key, different `schema_version` → conflict |
| Identity | the same UUID in two streams — both succeed, both readable, neither observes the other |
| Atomicity | intent insert fails → no log row, no head row; head upsert fails → no log row; batch of 3 where the 2nd conflicts → nothing written |
| Batch | two writes to the same aggregate chain correctly; ordering preserved in results |
| Reads | head after N writes; exact revision at every n; `history` paging with cursors; `history` on missing aggregate; `where` equality on top-level and dotted paths; missing path vs. explicit JSON null are distinct |
| Head | head digest equals the log digest at every revision |
| Time | `committed_at` is UTC, database-assigned, monotonic within a batch |
| Intents | staged in the same transaction; one function with two subscriptions produces two rows; claim/lease/ack; expired lease reclaimable; concurrent drainers (capability-gated) |
| Errors | every failure raises from the public tree; no driver exception escapes |

### Tests

The suite is the test. At this phase it must **fail to collect a backend** — there is
none yet — and a `NullStore` that raises `NotImplementedError` must fail every scenario
with a clear scenario name. That proves the runner reports honestly.

### Exit gate

`Capabilities` gates scenarios by *capability*, never by dialect name, and a
capability-gated skip is reported as a skip with a reason, not a pass.

---

## Phase 7 — SQL tables, statements, and the SQLite store

**Outcome:** the first real backend, passing the whole conformance suite.

### Steps

1. `sql/tables.py` — the four tables from `ARCHITECTURE.md` §6 as SQLAlchemy Core
   `Table` objects, with every check constraint, every index, and `.with_variant(...)`
   on every dialect-varying type. **This file is the single schema source.**
2. `sql/dialect.py` — a `Dialect` value object per backend carrying: JSON type, the
   path-equality expression builder, the upsert builder, the claim-locking clause, and
   a `Capabilities`.
3. `sql/statements.py` — **pure** builders returning SQLAlchemy Core constructs:
   `select_head`, `select_revision`, `select_window`, `insert_revision`, `upsert_head`,
   `select_history`, `search_heads`, `insert_intents`, `claim_intents`,
   `settle_intents`, `upsert_fingerprint`. This module executes nothing (R4).
4. `sql/store.py` — `SQLite(url_or_path, *, encodings=…)`. Only `.execute()` glue plus
   the §4.3 algorithm. Driver exceptions translated at the boundary.
5. Implement §4.3 step 5 exactly: derive the head **by decoding the row just encoded**,
   and assert the decoded digest equals `request.digest`, else abort with
   `EncodingError`. Write the test that breaks the encoder and proves the commit fails.
6. Wire `snapshot/1` only. `delta/1` waits for Phase 12.

### Tests

- The whole store conformance suite against SQLite, green.
- The head-derivation assertion: monkeypatch the encoder to drop a key; the commit must
  raise `EncodingError` and write nothing.
- `sql/statements.py` contains no `.execute`, no `Session`, and no connection (grep
  test).
- SQLite JSON path semantics: missing path and explicit null are distinguished, and a
  key containing a dot is addressable via escaping.

### Exit gate

Store conformance is green on SQLite, and `tests/architecture/test_async_ready.py`
(the R1/R2/R5/R10 subset) passes.

---

## Phase 8 — Runtime, dispatch, and the four-way property

**Outcome:** the first end-to-end vertical slice, and the single highest-leverage test
in the suite.

### Steps

1. `runtime.py`: `Runtime`, `Collection[T]`, `Batch`, `BatchCollection[T]` per
   `ARCHITECTURE.md` §2.3. `Collection` is pure delegation: plan (Phase 5) → one
   `store.commit` → hydrate → dispatch. `Batch` accumulates requests and issues one
   `commit` on `__exit__`; it exposes **no reads**.
2. `dispatch.py`: inline dispatch in declaration order, all handlers run, failures
   collected, `InlineDispatchError` raised or logged per `App.on_inline_error`.
3. Write the four-way agreement property (Hypothesis stateful):

   > For any sequence of `create` / `change` / `replace` / `batch` commands over
   > several aggregates and streams, at every step:
   > `digest(head) == digest(replay(log)) == digest(history[-1]) ==
   >  digest(returned Revision) == digest(rebuilt head)`.

   Include nested-container mutation in the command set: mutate a list on a returned
   `Revision.state` between commands and assert nothing durable changes.

### Tests

- The property above, `max_examples >= 300`, against SQLite.
- Inline handlers cannot observe a rolled-back commit: force the store to fail after
  the log insert and assert zero handler calls (I9).
- One failing inline handler does not prevent the others; the aggregate error names all
  failures.
- `Batch` has no `get`; the attribute does not exist (the read-your-writes question is
  unaskable, `CONCEPT.md` §10.3).

### Exit gate — **first vertical slice**

In a fresh process:

```python
ev = App(id="demo", streams=[todos]).bind(SQLite(":memory:"))
t = ev[todos].create(Todo(text="a"))
t = ev[todos].change(t, done=True)
assert ev[todos].get(t.id).digest == t.digest
assert [r.revision for r in ev[todos].history(t.id).items] == [0, 1]
```

works, and the four-way property is green. **Stop and reassess the design here if
anything in Phases 1–8 required a workaround.** Everything after this point is
plumbing on top of a core that is either right or wrong.

---

## Phase 9 — PostgreSQL, migrations, and schema parity

**Outcome:** the production backend, and one schema with two creation paths that cannot
drift.

### Steps

1. `Postgres(url, *, encodings=…)` reusing `statements.py` verbatim; only `dialect.py`
   differs (`JSONB`, `@>` containment plus explicit path equality, `FOR UPDATE SKIP
   LOCKED`, `concurrent_drainers=True`).
2. `sql/migrations/` — Alembic `env.py` targeting `tables.py` metadata, plus revision
   `0001_baseline` **generated** from it, never hand-written.
3. `sql/admin.py` — `migrate()` runs Alembic programmatically against the packaged
   script location; `check(app)` compares stored fingerprints and reports drift.
4. CI: a live Postgres service; the store conformance suite runs against both backends;
   `alembic check` runs against a database built by `alembic upgrade head` **and**
   against one built by `metadata.create_all`, and both must be clean.

### Tests

- Store conformance, green on Postgres, with `concurrent_drainers` scenarios now active.
- Schema parity: `create_all` and `alembic upgrade head` produce identical schemas on
  both dialects (this is 004/F04's root process failure, closed by gate rather than by
  care).
- JSONB does not break replay: write a revision whose payload has numerically
  equivalent but textually different numbers, and prove replay detection still works —
  because it compares digests, never the JSONB round trip (`CONCEPT.md` §10.4).
- A Postgres role with `INSERT`/`SELECT` only on `eventic_revision` can run the full
  write path (I1 survives direct database access).

### Exit gate

`alembic check` clean on both dialects in CI, conformance green on both, and the full
§0.2 command is now mandatory from here on.

---

## Phase 10 — Delivery: intents, worker, state machine

**Outcome:** durable at-least-once delivery with an honest contract.

### Steps

1. Intent staging in `commit` (already in the conformance suite from Phase 6 — now make
   it real end to end).
2. `worker.py`: the three-step loop from `ARCHITECTURE.md` §7.2 — short claim
   transaction, deliver **outside any transaction**, short settle transaction. Handler
   execution never holds a database lock (004/F25).
3. Event reconstruction: load the revision by `revision_id`, upcast, hydrate, build
   `Commit` with `changed` recomputed from the previous revision. Assert equality with
   the inline envelope in a test — field for field.
4. `retry.py` wired in: exponential backoff with cap, `max_attempts`, then
   `status='dead'` with a redacted, truncated `last_error`.
5. Structured results: `WorkerReport(claimed, delivered, retried, dead_lettered)`.

### Tests

- Delivery conformance suite on both backends: claim/lease/ack; crash after claim
  (lease expiry reclaims); crash after side effect (duplicate delivery — the
  at-least-once proof); ack failure; retry exhaustion → dead; redrive → pending;
  concurrent drainers deliver each intent once (Postgres) and the capability is
  correctly declared `False` on SQLite.
- One function registered under two subscriptions produces two intent rows and two
  deliveries, and does not violate a unique constraint (004/F14).
- Inline and durable `Commit` envelopes are field-for-field equal, including
  `committed_at` and `changed` (004/F10).
- A handler whose module cannot be imported by the worker fails the worker's app load
  with a non-zero exit, rather than silently retrying forever (004/F13).
- No credential, URL, or payload appears in `last_error` or any log line.

### Exit gate

Delivery conformance green on both backends, and a grep test asserts the string
"exactly once" appears in no source file, docstring, or document.

---

## Phase 11 — Schema evolution

**Outcome:** history stays readable across model changes.

### Steps

1. Enforce `schema_version` and `meta_version` on every write; upcast on every read
   path (`get`, `history`, `where`, worker reconstruction) via `evolution.py`.
2. `eventic_schema` fingerprint ledger: upsert once per process per
   `(stream, schema_version)`; `admin.check(app)` reports drift.
3. A fixture corpus under `tests/fixtures/evolution/`: databases (as SQL scripts, not
   pickles) containing rows written by earlier declared schema versions.

### Tests

- v1 rows read through a v2 stream produce correct v2 objects, at every read path
  including the worker's reconstruction.
- A missing upcaster is a *declaration* error (Phase 4), never a read-time surprise.
- Fingerprint drift — model changed, `schema_version` not bumped — is reported by
  `schema check` with a non-zero exit.
- Rolling upgrade: a v1 writer and a v2 reader against one database, concurrently.
- An upcaster with a side effect (clock, network) is impossible to write: the protocol
  passes only a JSON tree. Assert by signature test.

### Exit gate

`eventic schema check` exits non-zero on a drifted database and zero on a clean one,
on both backends.

---

## Phase 12 — `delta/1` and `eventic verify`

**Outcome:** the storage optimization, behind a suite that makes its historical failure
modes impossible.

### Steps

1. `encodings/delta.py` per `ARCHITECTURE.md` §5.2: top-level keys, explicit
   tombstones, checkpoint every `K` and always at revision 0.
2. Bounded-window reads: reconstruct revision *n* from `[checkpoint(n) … n]` in one
   range query, at most `K` rows.
3. `encodings/__init__.py`: the closed registry keyed by wire id. Unknown id on read →
   `UndecodableRevision` naming the id.
4. `testing/conformance/encoding.py`: scenarios asserting digest equality at every
   revision, across long histories, at checkpoint boundaries, with a corrupted row,
   with a missing checkpoint, and with a stream that switched encodings mid-life.
5. `admin.verify()` and `eventic verify`: stream the log in chunks, reconstruct every
   revision, compare against the stored digest, and compare the rebuilt head to the
   live head. Report counts and the first N mismatches.

### Tests

- Encoding conformance green for `snapshot/1` and `delta/1`.
- Field removal round-trips (the tombstone test — 003/F4 was exactly this).
- A point read at revision 800 with `K=20` touches at most 21 rows; assert by
  statement counting, not by timing (003/F17).
- Switching a stream from snapshot to delta and back leaves every historical revision
  readable, because `encoding` is per row.
- `eventic verify` on a database with a hand-corrupted payload exits non-zero and names
  the revision.

### Exit gate

`eventic verify` is clean on a database produced by the Phase 8 property test at
`max_examples=1000`, under both encodings.

---

## Phase 13 — CLI and operability

**Outcome:** every operator action is a documented command with a truthful exit code.

### Steps

1. `cli/loader.py`: `--app module:attr` imports and returns an `App`. A load failure is
   a clear error with a non-zero exit — never a silent no-op (004/F13).
2. Commands:

| Command | Behavior |
|---|---|
| `schema upgrade` | run migrations |
| `schema check` | fingerprint and structural drift; non-zero on drift |
| `heads rebuild [--stream S] [--chunk N]` | truncate scope in-transaction, rebuild, compare digests, report |
| `verify [--stream S]` | Phase 12 |
| `worker --queue Q [--once]` | drain; prints `WorkerReport`; non-zero if `dead_lettered > 0` |
| `intents list [--status dead]` | paged listing |
| `intents redrive --subscription ID` | dead → pending |
| `inspect` | resolved app: streams, schema versions, fingerprints, subscriptions with delivery and queue, store capabilities |

3. Exit codes: `0` success, `1` operational failure, `2` usage/config error, `3` drift
   detected.
4. `inspect` output must contain every fact that affects a commit. If a behavior is
   invisible there, it is a design bug.

### Tests

- Every command in a **fresh process** with only the installed package and an `--app`
  target. This is the test 0.3 failed (004/F13).
- `heads rebuild` removes orphan heads and leaves digests identical to pre-rebuild
  (004/F11).
- `worker --once` on an empty queue exits 0 and reports zeros; on a queue with an
  undeliverable intent it retries, then dead-letters, then exits non-zero.
- No command prints a connection URL or a payload.

### Exit gate

A shell script driving every command end-to-end against SQLite and Postgres, run in CI.

---

## Phase 14 — Release engineering

**Outcome:** the documented path works from an installed artifact.

### Steps

1. Wheel contents: `py.typed`, `sql/migrations/**`, `cli/**`, `testing/**`.
2. sdist excludes verified by test.
3. `docs/`: README (the §11 shape from `CONCEPT.md`), invariants, delivery semantics in
   precise words, evolution guide, operations guide, and a store-author guide pointing
   at the conformance suite.
4. Every README code block is an executed doctest and a `basedpyright` fixture.
5. Prune implementation-diary comments: source comments explain current invariants and
   non-obvious mechanics only (004/F32).

### Tests

- `tests/integration/wheel/`: build, install into an empty venv, then load an app,
  `schema upgrade`, write, drain, read back — using only the installed artifact.
- Minimal-dependency install (`pip install eventic`) imports and runs SQLite; the
  Postgres path fails with a clear "install `eventic[postgres]`" message.
- `import eventic` in a fresh interpreter leaves `sqlalchemy` out of `sys.modules`.

### Exit gate

The clean-wheel smoke test is green in CI on a runner with no project checkout.

---

## Phase 15 — Performance and capacity

**Outcome:** published complexity contracts, backed by measurement. Not before now.

### Steps

1. Benchmarks against live Postgres, not statement counts on SQLite: commit throughput
   and p99 latency; point read at revision 1/100/1000; `history` paging; `where` at
   10⁵ heads; drain throughput at 1/4/16 concurrent workers; `verify` and `rebuild` on
   10⁶ rows.
2. Optimize only what the benchmark shows, in this order: index shape → statement round
   trips → canonicalization cost (consider `verify="sampled"`) → batching.
3. Publish per-API complexity and memory contracts in the docs. `history`, `where`, and
   `verify` are paged/chunked; nothing materializes an unbounded result (004/F29).

### Exit gate

A committed benchmark report, and every public read API documented with its complexity
and its bound.

---

## Phase 16 — Async-readiness audit

**Outcome:** the async port is proven cheap before it is needed.

### Steps

1. Implement `tests/architecture/test_async_ready.py` in full (`ARCHITECTURE.md` §9.1).
2. Conduct a paper port: write the `AsyncStore` protocol signatures and the
   `sql/async_store.py` skeleton with `NotImplementedError` bodies, and confirm that
   `statements.py`, `planning.py`, `hydration.py`, `retry.py`, `canonical.py`, and
   `evolution.py` require **no edits**. Discard the skeleton; keep the finding.
3. If any module above the protocol line needed a change, that is a Phase-16 defect:
   fix the shape now, while there is one implementation to fix.
4. Record the measured port size in the docs so the future decision is informed.

### Exit gate

The audit test is green in CI, and the paper port required zero edits above
`protocols.py`.

---

## Milestone map

```
0 reset
└─1 leaves ─┬─2 canonicalization ─┬─3 declarations ─┬─4 App
            │                     │                 └─5 pure commit core ─┐
            │                     └─(type zoo)                            │
            └─6 store contract + conformance ───────────────────────────┬─┘
                                                                        │
                                        7 SQLite store ◄────────────────┘
                                              │
                                        8 runtime + four-way property   ★ vertical slice
                                              │
                     ┌────────────────────────┼────────────────────────┐
                     9 Postgres            10 delivery            11 evolution
                     + migrations              + worker              + fingerprints
                     └────────────────────────┼────────────────────────┘
                                        12 delta + verify
                                              │
                                        13 CLI ─ 14 release ─ 15 perf ─ 16 async audit
```

Phases 9, 10, and 11 are independent once Phase 8 lands and can be worked in parallel.
Nothing else can.

---

## Recommended commit sequence

Small, gated commits. One line each, no finding ids in messages.

```
 1  chore: reset to empty v1 package with quality gates
 2  feat: errors, ids, canonical json leaves
 3  feat: canonicalization with round-trip verification
 4  test: pydantic type zoo property
 5  feat: Stream, Meta, evolution chains
 6  feat: Revision/Commit/Page envelopes
 7  feat: App with collected declaration validation
 8  feat: wire types and pure commit planning
 9  feat: hydration and retry decisions
10  feat: Store protocol
11  test: store conformance scenarios and sync runner
12  feat: SQL tables, dialects, pure statement builders
13  feat: SQLite store passing store conformance
14  feat: Runtime, Collection, Batch, inline dispatch
15  test: four-way agreement property            ★ vertical slice
16  feat: Postgres store and generated baseline migration
17  ci: postgres matrix and alembic parity gate
18  feat: outbox worker with lease/retry/dead-letter
19  test: delivery conformance on both backends
20  feat: schema evolution and fingerprint ledger
21  feat: delta encoding and encoding conformance
22  feat: eventic verify
23  feat: cli with app loader and truthful exit codes
24  docs: README, invariants, delivery semantics, operations
25  test: clean-wheel smoke
26  perf: benchmarks and index tuning
27  test: async-readiness audit
```

---

## Mandatory validation matrix

Nothing ships until every row has a test that fails when the behavior regresses.

### State and Pydantic
Nested list/dict/model mutation after a commit changes nothing durable · computed
fields at every depth are absent from durable state · aliases, field and model
serializers, validators · `UUID`, `datetime` (aware and naive), `date`, `Decimal`,
`Enum`, `bytes`, `SecretStr`, `Path`, discriminated unions, deep nesting · field
reordering does not change a digest.

### Identity and concurrency
Same-base concurrent writers: one winner, all losers loud · stale, fabricated,
negative, and ahead-of-head revisions rejected · the same UUID in two streams · 8+
threads racing one `(stream, id, revision)` → exactly 1 winner (the 0.2 canary that
must never regress) · batch ordering and all-or-nothing.

### Persistence and rebuild
Head digest equals log digest at every revision · rebuild is byte-exact and removes
orphans · commit failure at each write boundary leaves nothing · database check
constraints reject invalid kind, negative revision, empty stream · `INSERT`-only role
can run the write path.

### Evolution
Old rows readable at every read path · missing upcaster is a declaration error ·
fingerprint drift detected · rolling upgrade · encoding switch mid-stream.

### Delivery
Inline and durable envelopes field-for-field equal · overlapping subscriptions ·
concurrent drainers · crash after claim and after side effect · retry exhaustion,
dead-letter, redrive · worker in a fresh installed-wheel process · no secrets anywhere.

### Release
Lint, format, types, warnings-as-errors, coverage · minimal install · wheel contents ·
migrations resolvable from the wheel · README commands executed from the artifact ·
SQLite and live Postgres.

---

## Final completion checklist

The fifteen statements in `CONCEPT.md` §12, each backed by a named test:

1. No eventic class in any user model's MRO.
2. Managed metadata cannot be supplied as state input.
3. A stale or fabricated revision raises rather than creating a gap.
4. Two streams may share an aggregate UUID.
5. Heads are byte-exactly rebuildable.
6. Rebuild changes no digest and leaves no orphan.
7. A commit writes everything or nothing.
8. Computed fields never enter durable state, at any depth.
9. Every row declares schema version and encoding.
10. `import eventic` imports pydantic only.
11. SQLite and Postgres pass one identical store conformance suite.
12. `snapshot/1` and `delta/1` pass one identical encoding conformance suite.
13. The installed wheel executes the documented production path.
14. No credential or payload appears in a row, an intent, an error, or a log line.
15. The async-readiness audit is enforced by CI, and "exactly once" appears nowhere.

---

## The guiding test

Apply this to every proposed addition, at every phase:

> **Can this be added without allowing two parts of the system to disagree about what
> was committed — and without requiring the framework to know anything about the
> user's class?**

If yes, it is a small pure function or a scenario in an existing suite. If no, it
belongs inside the sealed kernel, or it does not belong in 1.0.
