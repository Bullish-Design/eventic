# Eventic 1.0 — Remediation Guide

> Copy this entire file into a fresh session and follow it. It is the kickoff for
> `.scratch/projects/007-remediation/`, and it closes the sixteen findings in
> `.scratch/projects/006-implementation-review/REVIEW.md`.
>
> It follows the shape of `005-redesign/IMPLEMENTATION_GUIDE.md`: numbered phases,
> each with an **outcome**, ordered **steps**, the **tests it must produce**, and an
> **exit gate that is a command**, never a judgement.

---

## 0. Orientation

### 0.1 What happened before you

Eventic 1.0 was designed in `.scratch/projects/005-redesign/` and implemented on
branch `v1`. It was then reviewed (`006-implementation-review/REVIEW.md`), which
produced **16 findings, 2 of them release blockers**, and refuted 6 of the 14
hypotheses it started from. Your job is to close the findings — not to re-review,
not to redesign.

Read, in this order, before touching code:

1. `.scratch/projects/006-implementation-review/REVIEW.md` — the findings. This is
   your work list. Every finding has a reproduction.
2. `.scratch/projects/005-redesign/CONCEPT.md` §4 (invariants I1–I10) and §12
   (definition of done).
3. `.scratch/projects/005-redesign/ARCHITECTURE.md` §2.1 (declaration errors),
   §4.2–4.3 (the store contract and the required commit algorithm), §5.2
   (`delta/1`), §9 (async rules).

The probes that reproduce every finding live in
`.scratch/projects/006-implementation-review/probes/p01…p08`. **Run the relevant
probe before you fix anything and after — that is your proof.**

### 0.2 Environment

The repo is `devenv`-managed and colocated under `jj` (via gitman). Run
everything inside the shell:

```console
devenv shell -- uv run <command>
```

`uv` is not on `PATH` outside the shell. devenv's venv is `.devenv/state/venv`;
a legacy `.venv` also exists and is equivalent — do not delete either.

**The gate, unchanged from `IMPLEMENTATION_GUIDE.md` §0.2:**

```console
devenv shell -- uv run ruff check .
devenv shell -- uv run ruff format --check .
devenv shell -- uv run basedpyright
devenv shell -- uv run pytest -W error
```

Baseline at the start of this project (commit `ddff64e`):

```
All checks passed!
108 files already formatted
0 errors, 0 warnings, 0 notes
210 passed, 5 skipped
```

The 5 skips are `tests/conformance/test_postgres.py` — they need a live Postgres.
**Do not stay content with them skipped: Phase 2 exists precisely because a
blocker hides behind them.** See §0.4.

`repoman doctor` reports `FAIL installed:test — testee missing`. **Ignore it.**
The project deliberately keeps the original pytest/basedpyright gate so results
stay comparable across the review and this remediation. Do not add `testee` to
`pyproject.toml`.

### 0.3 Working discipline (inherited, non-negotiable)

1. **Write the failing test first, every time.** A fix without a test that fails
   before it is not a fix.
2. **Never weaken an assertion to make a test pass.** If a test is wrong, delete
   it and say why in the commit message.
3. **No test is named after a finding.** Name tests after behaviour.
   `test_replay_of_superseded_revision_leaves_head_alone`, never `test_f1`.
4. **Real databases only.** Never mock the database. File-backed SQLite (WAL) for
   anything concurrent; `:memory:` with `StaticPool` for single-threaded tests.
5. **Every warning is an error** (`-W error`). Do not add
   `@pytest.mark.filterwarnings` to silence something you introduced.
6. **Do not modify** `.scratch/projects/00{1,2,3,4}` — they are read-only archive
   (mode `555`). Note: this breaks tools that copy the tree and then delete files,
   which is why `copyroom adopt` crashed during the review.
7. Commit per phase, small and gated. Use plain `git` or `gitman`; both work
   (the repo is colocated).

### 0.4 Getting Postgres

Two findings (F2, F7) cannot be *proved* fixed without a live Postgres. Before
Phase 2, get one:

```console
# option A — devenv (preferred; add to devenv.nix, then re-enter the shell)
services.postgres.enable = true;
services.postgres.initialDatabases = [{ name = "eventic"; }];

# option B — docker
docker run -d --rm -e POSTGRES_PASSWORD=x -p 5432:5432 postgres:17

export EVENTIC_PG_URL=postgresql+psycopg://postgres:x@localhost:5432/postgres
devenv shell -- uv run pytest -W error tests/conformance/test_postgres.py -v
```

Those 5 tests must **pass**, not skip, before you claim Phase 2 is done.

---

## Phase 1 — F1: replay must not rewind the head

**Severity:** blocker. **Outcome:** replaying a superseded revision is a no-op on
the head.

### The defect

`src/eventic/sql/store.py:171-181`. When a log row already exists at the target
revision and `_is_identical` holds, the code calls
`_upsert_head_from_row(conn, existing, ...)` (line 173), which upserts the head
from **that** row. `Dialect.upsert_head` (`sql/dialect.py:118`) is an
unconditional `ON CONFLICT DO UPDATE`, so a head at revision 2 is overwritten by
revision 1's state, digest and `revision_id`.

`head_row` is already in scope from line 156. Nothing compares them.

### Steps

1. Write the failing test first, in `tests/conformance/test_store_contract.py`
   (or a new `tests/conformance/test_replay.py`):

   > create → change → change (head is revision 2). Re-commit the revision-1
   > request verbatim. Assert `result.replayed is True`, `head.revision == 2`,
   > `head.digest == log[2].digest`, and that the log still has exactly 3 rows.

   Confirm it fails against current `main`.

2. In `_commit_one`, guard the head write in the replay branch. The head should
   only move **forward**:

   ```python
   if existing is not None:
       if self._is_identical(existing, request):
           if head_row is None or head_row["revision"] < existing["revision"]:
               self._upsert_head_from_row(conn, existing, request, rid, now)
           return CommitResult(..., replayed=True)
       raise RevisionConflict(...)
   ```

   Keep the `head_row is None` arm: it is what repairs a missing head, and
   `tests/conformance/test_admin.py` depends on that behaviour.

3. Add the missing conformance scenario to the `REPLAY` group
   (`src/eventic/testing/conformance/scenarios.py:120`), so **both** backends
   assert it:

   ```python
   Scenario(
       "replay of a superseded revision leaves the head alone",
       steps=(
           _commit("todos", _A, None, "create", _DOC1),
           _commit("todos", _A, 0, "change", _DOC2),
           _commit("todos", _A, 1, "change", _DOC3),
           _commit("todos", _A, 0, "change", _DOC2,
                   expect_replayed=True, expect_revision=1),
           head_step("todos", _A, expect_revision=2,
                     expect_digest=digest(canonical_bytes(_DOC3))),
       ),
   ),
   ```

### Tests

- The scenario above, green on SQLite (and on Postgres after Phase 2).
- The unit test from step 1.
- `probes/p02_replay_rewinds_head.py` must now **fail its assertions** — it
  asserts the bug. Update it in place to assert the fixed behaviour and note in
  its docstring that it is a regression probe.

### Exit gate

```console
devenv shell -- uv run python .scratch/projects/006-implementation-review/probes/p02_replay_rewinds_head.py
devenv shell -- uv run pytest -W error
```

The probe reports `head revision : 2` after the replay, and the suite is green.

---

## Phase 2 — F2 + F7: lock the CAS, map the constraint, race both backends

**Severity:** blocker. **Outcome:** a lost race raises `RevisionConflict` on both
backends, proven by the canary running against both.

### The defect

Three parts, all in `src/eventic/sql/`:

- `store.py:158` — the CAS read passes `for_update=False`. It is the only CAS
  call site, and no call site anywhere passes `True`, so
  `statements.py:31`'s `with_for_update()` branch is unreachable.
- `store.py:701` — `Postgres._install_events` is `pass`. No `BEGIN IMMEDIATE`
  equivalent, default READ COMMITTED, so two writers with the same
  `expected_revision` both pass the CAS and both INSERT.
- `store.py:145` — `except Exception → StoreError("commit failed")`. There is no
  `IntegrityError` arm, so the unique-constraint backstop can never produce
  `RevisionConflict`, contradicting `ARCHITECTURE.md` §4.3 step 1.

### Steps

1. **Lock the CAS read.** In `_commit_one`, pass `for_update=True`. SQLAlchemy
   emits `FOR UPDATE` on Postgres and silently ignores it on SQLite, so one line
   covers both:

   ```python
   st.select_head(request.stream, request.aggregate_id, for_update=True)
   ```

   Leave `head()` (line 358) at `for_update=False` — it is a read path.

   **Careful:** `SELECT … FOR UPDATE` locks an *existing* row. It does **not**
   prevent two concurrent `create`s of the same new aggregate, because there is
   no row to lock. That case is covered by step 2, which is why both are required.

2. **Map the constraint violation.** Inside `commit()`, before the generic
   handler:

   ```python
   except EventicError:
       raise
   except sqlalchemy.exc.IntegrityError as exc:
       raise RevisionConflict(
           "concurrent write to the same revision",
           stream=..., aggregate_id=..., revision=...,
       ) from exc
   except Exception as exc:
       raise StoreError("commit failed") from exc
   ```

   The batch may hold several requests, so carry enough context to name the
   offending one, or omit the structured fields rather than guess.

3. **Delete the stale placeholder.** `tests/conformance/test_postgres.py:73`,
   `test_concurrent_drainers_scenario_active`, asserts `assert not names` — that
   concurrency scenarios *do not exist* — with the comment "land in Phase 10".
   Phase 10 shipped. Delete the test; it currently certifies a gap.

4. **Parameterise the race canary over a store factory.**
   `tests/conformance/test_race_canary.py` hardcodes `SQLite(...)` in
   `_make_store` (line 20). Refactor to take a factory, then run it against both
   backends — SQLite always, Postgres when `EVENTIC_PG_URL` is set. Keep both
   encodings.

5. **Add capability-gated concurrency scenarios** to the declarative suite so
   backend parity is enforced by data, not by a bespoke test: concurrent
   drainers (`requires={"concurrent_drainers"}`), and a same-`expected_revision`
   race. Gate by capability, never by dialect name.

### Tests

- The canary, green on SQLite and Postgres: 1 winner, N−1 `RevisionConflict`,
  zero `other:` outcomes. The canary already asserts `not other` — make sure that
  assertion is what catches a `StoreError` regression.
- A test that a unique-constraint violation surfaces as `RevisionConflict`.
  `probes/p07_cas_race_mapping.py` shows how to force the interleaving
  deterministically on SQLite.

### Exit gate

```console
export EVENTIC_PG_URL=...
devenv shell -- uv run pytest -W error tests/conformance/test_race_canary.py tests/conformance/test_postgres.py -v
```

All pass, **none skip**.

---

## Phase 3 — F3: `replace` computes `changed` against the wrong document

**Severity:** major. **Outcome:** inline and durable `Commit` envelopes agree for
`replace`, as `ARCHITECTURE.md` §2.2 promises.

### The defect

`src/eventic/runtime.py:77` — `replace` passes the **new** state as the `before`
argument, so `_changed` diffs the new document against itself and always yields
`frozenset()`. The worker reconstructs the true diff from the log
(`worker.py:119`), so the two envelopes for the same commit disagree.

### Steps

1. Extend the existing inline-vs-durable envelope-equality test (in
   `tests/conformance/test_worker.py`) to cover `replace` alongside `create` and
   `change`. Confirm it fails.
2. Fix `Collection.replace`:

   ```python
   return self._commit_one(request, base.state)
   ```

   Do the same in `BatchCollection.replace` (`runtime.py:243-251`), which passes
   `state` to `_batch._add`; it must pass `base.state`.
3. Check `Collection.change` (line 67) already passes `base.state` — it does.

### Tests

- Envelope equality across `create`, `change` **and** `replace`, field for field.
- `probes/p01_replace_changed.py` must now report the same `changed` set for the
  inline and worker paths. Update its assertions.

### Exit gate

```console
devenv shell -- uv run python .scratch/projects/006-implementation-review/probes/p01_replace_changed.py
```

`replace changed` equals `worker changed`.

---

## Phase 4 — F4: make the purity test see the bug it exists to catch

**Severity:** major. **Outcome:** a module-level mutable cannot be added without
the suite failing, whatever it is named.

### The defect

`tests/architecture/test_no_global_state.py:29` skips every attribute whose name
starts with `_`. The three globals that caused this library's entire redesign
were named `_ENGINE`, `_CURRENT` and activation tokens (003/F8, 003/F10,
004/F07, 004/F16). Injecting `planning._CURRENT_STORE = {}` leaves the suite
green — see `probes/p06_enforcement_gaps.py`.

### Steps

1. Remove the `_`-prefix exemption.
2. Run the suite; it will now flag legitimate private module constants. For each,
   decide honestly:
   - genuinely immutable → convert to `tuple` / `frozenset` /
     `types.MappingProxyType` and let the test assert immutability rather than
     name shape;
   - genuinely a cache → that is the bug; remove it.
3. Only if a binding is provably immutable and cannot be expressed as such,
   add it to a short, explicit, commented allowlist inside the test. An
   allowlist of names you must justify is fine; a wildcard on `_` is not.
4. Strengthen the check while you are there: also reject module-level objects
   with mutable attributes, and class-level mutable defaults, or state in the
   test's docstring that they are out of scope and why.

### Tests

- The existing test, with the exemption gone.
- A new test asserting the scan *catches* an injected private mutable — build it
  the way `probes/p06_enforcement_gaps.py` does: inject into a real module,
  assert the scan reports it, remove it. This is the test that keeps the guard
  honest.

### Exit gate

```console
devenv shell -- uv run python .scratch/projects/006-implementation-review/probes/p06_enforcement_gaps.py
```

The injected `planning._CURRENT_STORE` is now reported as an offender.

---

## Phase 5 — F5: make the memory claims true, or make the claims honest

**Severity:** major. **Outcome:** `docs/BENCHMARKS.md` describes what the code
does.

### The defect

`src/eventic/sql/admin.py:52` — `_stream_log` reads the log in chunks but folds
every row into `final: dict[(stream, aggregate_id) -> full document]` and returns
it whole. Called by `rebuild_heads` (line 144) and `verify` (line 209). Peak
memory is `O(aggregates × document)`, independent of `chunk`.

`docs/BENCHMARKS.md` claims *"bounded memory per chunk"* and *"nothing
materializes an unbounded result"*. `SqlAdmin.list_intents` (line 227) is
docstringed "Paged-listing support" and has no limit at all.

### Steps

Pick **one** of these and do it completely:

**Option A — make it true (preferred).** The log query is already ordered by
`(stream, aggregate_id, revision)` (`statements.py:218`). An aggregate's rows are
therefore contiguous, so a document can be finalised and released the moment the
aggregate key changes. Restructure `_stream_log` into a generator-free fold that
yields completed `(key, doc)` pairs to a callback, keeping at most one in-flight
document plus one chunk of rows. Both callers become streaming loops.

> Note R2/§9: no generator may cross the **store protocol** boundary. `_stream_log`
> is a private helper inside `sql/admin.py`, well below that line, so a callback
> or an internal generator here does not violate the async rules. Keep
> `StoreAdmin`'s four public methods returning plain report values.

**Option B — make the claim honest.** Change the BENCHMARKS row to
`O(aggregates × document)` in memory, and say so in `docs/OPERATIONS.md` next to
the `heads rebuild` / `verify` commands, with guidance to scope by `--stream`.

Either way:

1. Give `list_intents` a `limit` and a cursor, and wire `intents list --limit`
   through `cli/commands.py`. It is the one table that grows without bound when a
   worker stalls, so this is not hypothetical.
2. `statements.select_all_log_for` uses `OFFSET`, which is `O(n²)` over a large
   log regardless of which option you pick. Convert it to keyset paging on
   `(stream, aggregate_id, revision)`.

### Tests

- A test asserting `verify` peak memory does not scale with aggregate count
  (Option A), using `tracemalloc` as `probes/p05_time_atomicity_bounds.py` does —
  or, for Option B, a docs test is not possible, so instead assert the documented
  bound in `docs/BENCHMARKS.md` matches a measured value at two sizes.
- `list_intents` respects `limit` and its cursor round-trips.

### Exit gate

```console
devenv shell -- uv run python .scratch/projects/006-implementation-review/probes/p05_time_atomicity_bounds.py
```

Either the peak no longer scales with aggregate count, or `docs/BENCHMARKS.md`
states the real bound (and you can point at the line).

---

## Phase 6 — F6: the declaration error taxonomy

**Severity:** major. **Outcome:** the error classes `ARCHITECTURE.md` §2.1
documents are either raised or deleted.

### The defect

`DuplicateId`, `UnknownStream` and `UnsupportedHandler` are defined in
`errors.py`, documented in §2.1's table and §8's tree, and **raised nowhere**.
`App._validate` (`app.py:121`) raises a bare `ConfigError` for every failure.
`tests/unit/test_errors.py` asserts only that the classes exist and subclass
`ConfigError` — taxonomy, not behaviour — so no test could have caught it.

### Steps

Decide, and write the decision in the commit message:

**Option A — raise them (preferred; matches the spec).** §2.1 also requires that
*all* failures are collected and reported together. Reconcile the two:

1. Collect `(error_class, message)` pairs instead of bare strings.
2. If exactly one distinct class is present, raise that class with all its
   messages joined by newline. If several, raise `ConfigError` with all messages
   — the common base is the honest type when the failures are heterogeneous.
3. Keep the existing "one error listing all failures" test green; add tests
   asserting the specific class for each single-fault case.

**Option B — delete them.** Remove the three classes, and correct §2.1's table
and §8's tree in `ARCHITECTURE.md`. Cheaper, but it removes a genuinely useful
API distinction.

Either way, **fix `tests/unit/test_errors.py`**: asserting a class exists and
subclasses its parent is not a test. Replace those assertions with ones that
construct the condition and check what is raised.

### Exit gate

```console
devenv shell -- uv run python .scratch/projects/006-implementation-review/probes/p03_declaration_contract.py
```

The four `spec=` / `actual=` columns agree (Option A), or the probe and the
architecture doc are updated together (Option B).

---

## Phase 7 — F8: the fifth leg and the delta parameterisation

**Severity:** major. **Outcome:** the four-way property is actually five-way, and
runs under both encodings.

### The defect

`tests/property/test_four_way.py:57` — `_check` verifies head, replay, `history[-1]`
and the returned revision. `IMPLEMENTATION_GUIDE.md` Phase 8 names a fifth leg,
`digest(rebuilt head)`, which is never exercised: `admin.rebuild_heads` is not
called. Separately, line 51 constructs `SQLite(":memory:")` with no `encodings`,
so all 300 examples run under `snapshot/1` — `delta/1`, the encoding with a
correctness bug in every prior version, has no property coverage.

The rebuild behaviour itself is correct (verified in `probes/p04_delta.py`), so
this is a coverage fix, not a bug fix. Do it anyway: it is the artifact
`ARCHITECTURE.md` §11 calls the highest-leverage test in the suite.

### Steps

1. Add the fifth leg to `_check`:

   ```python
   report = self.store.admin().rebuild_heads(None, chunk=17)
   assert report.mismatches == 0
   rebuilt = self.store.head(key)
   assert rebuilt is not None and rebuilt.digest == revision.digest
   ```

   Rebuilding on every step is slow; if `max_examples=300` becomes intolerable,
   rebuild in the `@invariant()` instead of `_check`, and pick a chunk size
   smaller than the aggregate count so the fold crosses chunk boundaries.

2. Parameterise the machine over `{None, {"todos": Delta(every=5)}}`. A small
   `every` is deliberate: it forces checkpoint boundaries inside short histories.

3. `mutate_nested_list` (line 112) asserts `head.payload["tags"] == []`, which
   assumes the aggregate was never given tags. Confirm that still holds under the
   new command interleavings, and tighten it to compare against the known
   revision rather than a literal if not.

### Exit gate

```console
devenv shell -- uv run pytest -W error tests/property/test_four_way.py -v
```

Green under both encodings, with the rebuild leg active.

---

## Phase 8 — the minor findings

Each is small and independent. One commit each.

### F9 — `Meta.__eq__` ignores `version`

`src/eventic/meta.py:50-56` compares `self.model is other.model` only, so
`Meta(Todo, version=1) == Meta(Todo, version=2)` and their hashes match. `App`
equality delegates to it, so two apps with different metadata versions compare
equal.

Include `version` in both `__eq__` and `__hash__`. `Stream.__eq__` being
name-only is deliberate and documented — leave it, but add a sentence to
`Stream`'s docstring noting that `App` equality is therefore
identity-of-declaration, not equivalence-of-behaviour.

### F10 — `committed_at` precision and the missing Time scenario

`_now()` compiles to `CURRENT_TIMESTAMP` on SQLite: whole seconds, one reading
per transaction. "Monotonic within a batch" holds only non-strictly.

1. State the real guarantee in `docs/INVARIANTS.md` / `docs/OPERATIONS.md`: UTC,
   database-assigned, **non-decreasing**, and explicitly *not* a sort key —
   order by `revision`.
2. Add the missing **Time** scenario. The group at
   `scenarios.py:448` is named `HEAD_TIME` but contains only the Head scenario,
   which hides the gap. Assert: tz-aware UTC, non-decreasing across a batch, and
   equal for all requests within one commit.

### F11 — `Worker.run_forever` cannot be stopped

`src/eventic/worker.py:69` is `while True` with a blocking sleep and no exit path.

Add a `threading.Event` stop flag checked each iteration, and a `stop()` method.
Install the `SIGTERM`/`SIGINT` handler in `cli/commands.py`, **not** in the
library — a library that installs signal handlers is a library that breaks its
host. Also hoist the `import time` to module scope.

### F12 — `schema check` mutates the database

`src/eventic/sql/admin.py:114` inserts the declared fingerprint when the ledger
row is missing and reports `ok=True`. Drift *is* caught once a baseline exists
(the review confirmed this), so the claim mostly holds; the problems are that a
command named `check` writes to production, and that on a never-written database
the first check defines rather than verifies.

Make `check` read-only. Report a missing ledger row as a third state — "no
baseline recorded" — distinct from clean and from drift. Decide its exit code
deliberately (`0` with a warning is defensible; `3` is not, since it is not
drift) and document it in `docs/OPERATIONS.md`.

### F13 — `changed_keys` cannot report a removed key

`src/eventic/planning.py:117` iterates `after` only, so a key in `before` and not
in `after` is invisible. Unreachable for a fixed model; reachable for
`extra="allow"` streams and top-level `dict[str, Any]` fields.

```python
if before is None:
    return frozenset(after)
return frozenset(before.keys() ^ after.keys()) | frozenset(
    k for k in after.keys() & before.keys() if before[k] != after[k]
)
```

`delta/1` already handles removal correctly via tombstones — this is a
`Commit.changed` fix only. Add a test with an `extra="allow"` model.

### F14 — the `"exactly once"` grep misses `README.md`

`tests/architecture/test_delivery_contract.py:13` scans `src/**/*.py` and
`docs/**/*.md`. `README.md` is at the repo root — the primary documentation, and
the only file whose code blocks run as doctests.

Add `ROOT.glob("*.md")`. Note this will also pull in `AGENTS.md` and
`CLAUDE.md` (a symlink to it) — dedupe by `Path.resolve()` so the symlink is not
read twice.

### F15 — `statements.claim_intents` is dead and broken

`src/eventic/sql/statements.py:100` builds an `update(intents)` whose
`.returning()` selects columns of `eventic_revision`, a table the statement never
joins. It has no call site — `SQLite.claim` uses `dialect.claim_select` plus
`claim_mark_leased`.

Delete it.

### F16 — a newer `schema_version` is read silently by an older declaration

`src/eventic/hydration.py:57` returns the tree unchanged when
`from_version >= to_version`. A v2 row read by a process declaring
`schema_version=1` is validated against the v1 model with no warning; Pydantic's
default `extra="ignore"` drops the new keys and the `Revision` looks valid. Every
rolling deploy produces this window.

Raise `UndecodableRevision` when `stored.schema_version > stream.schema_version`,
naming both versions and the stream. Add the reverse-direction rolling-upgrade
test: a v2 writer and a v1 reader against one database.

### Exit gate for Phase 8

```console
devenv shell -- uv run pytest -W error
devenv shell -- uv run python .scratch/projects/006-implementation-review/probes/p03_declaration_contract.py
devenv shell -- uv run python .scratch/projects/006-implementation-review/probes/p08_paging_and_schema_check.py
```

---

## Phase 9 — close the conformance-table gaps

**Outcome:** every row of the `IMPLEMENTATION_GUIDE.md` Phase 6 table has a
scenario, and the Phase 10 and Phase 12 tables have been audited.

### Steps

1. Add the scenarios the review found missing (beyond F1's and F10's, added
   above):
   - **Atomicity**: *head upsert fails → no log row*. The behaviour is already
     correct (`probes/p05` proves it) but has no scenario. Model it as a scenario
     with an injected failure, or — if the scenario DSL cannot express that —
     add it as a conformance test next to the suite and say so in a comment.
   - **Batch**: *ordering preserved in results*.
   - **Errors**: *every failure raises from the public tree*. F2 was a live
     counter-example; make it a standing assertion.
2. **Audit the Phase 10 (delivery) and Phase 12 (encoding) tables row by row.**
   The 006 review audited Phase 6 only and explicitly left these two unfinished.
   Produce the same table-vs-`scenarios.py` gap list and close what is missing.
3. Re-check `CONCEPT.md` §12 item by item against the tests that claim to prove
   it. The review's table lists items 5, 6, 11, 13 and 15 as needing attention;
   Phases 1, 2, 7 and 8 above should have moved most of them.

### Exit gate

A short `.scratch/projects/007-remediation/COVERAGE.md` mapping every Phase 6,
10 and 12 table row to a scenario name, with no row unmapped.

---

## Phase 10 — verification and close-out

### Steps

1. Full gate, with Postgres live:

   ```console
   export EVENTIC_PG_URL=...
   devenv shell -- uv run ruff check .
   devenv shell -- uv run ruff format --check .
   devenv shell -- uv run basedpyright
   devenv shell -- uv run pytest -W error
   ```

   Expect **215 passed, 0 skipped** (210 + the 5 Postgres tests now running),
   plus whatever you added.

2. Re-run all eight review probes. Each must now demonstrate the fixed
   behaviour. Update each probe's docstring to say what it now guards.

3. Write `.scratch/projects/007-remediation/OUTCOME.md`: finding → commit →
   test that now proves it. For anything you decided *not* to fix, record the
   decision and the reason. A finding closed by an argument is fine; a finding
   closed silently is not.

4. Re-read the 006 verdict's two falsified claims and confirm, in writing, that
   each is now true — with the command that shows it.

### Exit criteria

- [ ] F1 and F2 fixed, each with a conformance scenario running on both backends.
- [ ] The 5 Postgres tests pass rather than skip.
- [ ] The race canary runs against SQLite **and** Postgres.
- [ ] The four-way property has five legs and runs under both encodings.
- [ ] `test_no_global_state.py` catches an injected private module-level mutable.
- [ ] Every F1–F16 finding is closed or has a written, defended decision.
- [ ] Every Phase 6/10/12 table row maps to a scenario in `COVERAGE.md`.
- [ ] The full gate is green with no new `filterwarnings` or `type: ignore`.
- [ ] `OUTCOME.md` exists and is committed to `v1`.

---

## Appendix — findings at a glance

| # | Severity | Where | Phase |
|---|---|---|---|
| F1 | blocker | `sql/store.py:171-181` replay rewinds the head | 1 |
| F2 | blocker | `sql/store.py:158,145,701` no CAS lock, wrong error | 2 |
| F3 | major | `runtime.py:77` `replace` diffs against itself | 3 |
| F4 | major | `test_no_global_state.py:29` skips `_`-prefixed | 4 |
| F5 | major | `sql/admin.py:52` unbounded fold; false docs claim | 5 |
| F6 | major | `app.py:121` error taxonomy never raised | 6 |
| F7 | major | concurrency coverage is SQLite-only | 2 |
| F8 | major | `test_four_way.py:51,57` four legs, snapshot-only | 7 |
| F9 | minor | `meta.py:50` equality ignores `version` | 8 |
| F10 | minor | `committed_at` precision; no Time scenario | 8 |
| F11 | minor | `worker.py:69` no graceful shutdown | 8 |
| F12 | minor | `sql/admin.py:114` `check` mutates | 8 |
| F13 | minor | `planning.py:117` misses removed keys | 8 |
| F14 | minor | `test_delivery_contract.py:13` skips `README.md` | 8 |
| F15 | minor | `sql/statements.py:100` dead broken builder | 8 |
| F16 | minor | `hydration.py:57` newer schema read silently | 8 |

**Refuted in 006 — do not re-litigate:** R4 (atomicity holds), R5 (`head()`'s
`kind` is not observable), R6 (delta reserved keys are safe — the envelope nests
the document under `set`), R9 (point-read bound holds), R12 (keyset paging is
stable), R13 (`make_upcaster` identity is latent only). Evidence for each is in
`006-implementation-review/REVIEW.md` §Refuted candidates.
