# Eventic 1.0 — Implementation Review Prompt

> Copy this entire file into a fresh session and follow it. It is the kickoff
> for `.scratch/projects/006-implementation-review/`. It follows the precedent
> of `001-code-review`, `003-structural-refactor`, and
> `004-structural-refactor-review` (each produced a `REVIEW.md` with verified,
> numbered findings).

---

## 0. Your mandate

Review the **eventic 1.0 implementation on branch `v1`** adversarially and
thoroughly, against its own design documents. You are the fourth reviewer of
this library. The prior three reviews (001, 003, 004) produced ~70 verified
findings; two of them were release-blocking structural failures. Your job is
to decide whether 1.0 actually delivers what `005-redesign` promises — not
whether it passes its own tests. **A finding is the deliverable.** Nothing is
"fine because it passes its own suite"; the suite is also under review.

The design is explicit about its own bar:

> "Every release blocker in 004 is an instance of computing the same thing
> twice (I3) or failing to make the writes atomic and store-scoped (I8)."

Judge 1.0 against that bar.

## 1. Current state (verify before you start)

- Working directory: `/home/andrew/Documents/Projects/eventic`
- Branch: `v1` (up to date with `origin/v1`). Baseline: commit `54defd6`
  (verify `git log --oneline -1` matches or is newer; the review is of `v1`
  as-is, not of a modified tree).
- The tree should be clean except untracked files under `.scratch/`.
- Run the documented gate and record the results in your report:
  ```console
  uv run ruff check .
  uv run ruff format --check .
  uv run basedpyright
  uv run pytest -W error
  ```
  Expect ~210 passed, 5 skipped — the 5 skips are live-Postgres tests
  (CI-only; they must be reviewed statically and listed as unverified if you
  cannot run Postgres). Note: `uv` lives under the nix store, not on PATH;
  use `uv run` from the repo root (the venv exists).

## 2. Read these, in order

1. `.scratch/projects/005-redesign/README.md` — the project brief and the
   "must not regress" canary.
2. `.scratch/projects/005-redesign/CONCEPT.md` — the thesis, the ten
   invariants (I1–I10), §12 definition of done.
3. `.scratch/projects/005-redesign/ARCHITECTURE.md` — the module graph, wire
   types, the seven-method `Store` contract (§4), the physical schema (§6),
   the ten async rules (R1–R10), §9.1 enforcement test.
4. `.scratch/projects/005-redesign/IMPLEMENTATION_GUIDE.md` — the seventeen
   phases, the exit gates (each is a command), the mandatory validation
   matrix, and the final completion checklist.
5. `.scratch/projects/003-structural-refactor/REVIEW.md` and
   `.scratch/projects/004-structural-refactor-review/REVIEW.md` — the evidence
   base (003/F4, 004/F01 style references) and the review format to emulate.
6. `docs/` — the shipped claims (INVARIANTS, DELIVERY, EVOLUTION,
   OPERATIONS, STORE_AUTHORS, ASYNC_READINESS, BENCHMARKS) and `README.md`.
   Every sentence that states a guarantee is a claim to verify.

## 3. Review procedure

Work in this order. Record everything; nothing is throwaway.

### 3.1 Spec-fidelity audit

Diff the implementation against ARCHITECTURE.md **type by type, method by
method, column by column**:

- The seven `Store` methods and their signatures (`protocols.py` vs §4.2).
- The wire types (`wire.py` vs §4.1) — every extra or changed field.
- The physical schema (`sql/tables.py` vs §6): columns, constraints, indexes.
- The module graph and the §1.1 dependency rules (`tests/architecture/`).
- The public surface (`eventic/__init__.py`, `runtime.py` vs §2.3).

Every deviation is either (a) a justified design decision with a written
reason you can defend, or (b) a finding. **Known deliberate deviations to
judge** (verify each with code, and decide):

1. `CommitRequest` carries a `fingerprint` field not in §4.1 (it exists so the
   `eventic_schema` ledger can be written at commit time). The ledger is
   upserted on **every** commit (`ON CONFLICT DO NOTHING`), not "once per
   process per pair on first commit" (§6.4), and `schema check` seeds missing
   rows and passes — so drift is only caught on the **second** check. Judge
   whether that weakens "caught at deploy time rather than at read time".
2. `ClaimedIntent` gained `stream` / `aggregate_id` / `revision`; `claim`
   now JOINs the log row to return the aggregate key (the protocol stayed at
   seven methods). Check §7.2's "load the revision by revision_id" against
   what the worker actually does.
3. `App.streams` / `subscriptions` are declared `Sequence` (not `tuple`,
   §2.1) and normalized to tuples in the validator.
4. SQLite uses WAL + `BEGIN IMMEDIATE` + `busy_timeout` +
   `wal_autocheckpoint` (§6.5 declares "BEGIN IMMEDIATE + single-drainer"
   only for claims). Verify the concurrency claims this enables.
5. Delta payloads carry an extra `every` key beyond §5.2's
   `{"base","set","del"}` shape (needed for windowing). **Check the reserved-key
   hazard: a user document whose top-level keys are `set`, `del`, `base`, or
   `every` under a delta-encoded stream.**
6. `head()` returns `StoredRevision.kind = "change"` for every head read (the
   head table has no `kind` column, §6.2). Trace whether that lie is
   observable anywhere (worker reconstruction, `Commit` envelopes, `history`).
7. `Collection.replace` (see §3.4 probe R3 — suspected bug).

### 3.2 Exit-gate re-verification

Run the phase gates that are commands, and report pass/fail verbatim:

- Phase 8 vertical slice (the four-way property test and the slice in
  `tests/property/test_four_way.py`).
- Phase 9: `alembic check` clean on both creation paths (static review if no
  Postgres).
- Phase 12: `eventic verify` clean under both encodings
  (`tests/conformance/test_encodings.py`).
- Phase 14: clean-wheel smoke (`tests/integration/wheel/`) — note it runs in
  the project checkout, so the CI claim "runner with no project checkout" is
  only statically verified.
- Phase 16: the async-readiness audit and the paper port
  (`.scratch/projects/005-redesign/async-port/`).

### 3.3 Invariant probes (I1–I10)

For each invariant, attempt to construct a violation. Concrete probes to try:

- **I1 append-only** — does any public path or admin command mutate or delete
  a log row? Check `admin.rebuild_heads` (it deletes heads, never log rows —
  verify), and the `-W error`-gated suite under direct SQL.
- **I2 log is the only truth** — corrupt a head row and run
  `heads rebuild`; verify the digest comparison and orphan removal are
  byte-exact (004/F11). Check the delta path specifically (chunked rebuild
  across checkpoint boundaries).
- **I3 one canonical document** — the commit path derives the head by
  decoding the row just encoded (§4.3 step 5). Verify the digest assertion
  actually aborts on an encoder bug (the existing test monkeypatches a broken
  encoder — does it fail loudly and write nothing?).
- **I4 pure declaration** — `import eventic` must not import sqlalchemy;
  constructing any declaration must not touch I/O or globals. Verify the
  `tests/architecture/test_no_global_state.py` and `test_import_graph.py`
  scans are actually comprehensive (do they catch a new module-level dict?
  a new `import sqlalchemy` at module level in a leaf?).
- **I5 explicit store-bound writes** — is there *any* path that writes
  without a bound `Collection`? Grep for direct `store.commit` calls in
  `src/eventic/` outside `sql/`.
- **I6 deterministic identity** — pinned vectors in `tests/unit/test_ids.py`;
  verify the store and the runtime compute `revision_id` identically (a batch
  create+change, a replay, a worker reconstruction).
- **I7 loud conflicts** — race the CAS; the canary is
  `tests/conformance/test_race_canary.py` (8 threads, 1 winner, 7 loud).
  Verify it under both encodings and with more writers. **Verify replay
  detection compares digests and meta, never the JSONB round trip** (the
  Postgres path is CI-only — inspect `_is_identical` and the dialect's JSON
  handling for dialect-vs-logic coupling, 004/F21).
- **I8 atomic commit** — force failures at each write boundary: intent
  insert, head upsert, fingerprint upsert, mid-batch conflict. The suite has
  two of these; check the table in IMPLEMENTATION_GUIDE §Phase 6 against the
  authored scenarios (see §3.5).
- **I9 post-durability dispatch** — verify inline handlers cannot observe a
  rolled-back commit (the test wraps a store whose `commit` raises after the
  real commit). Check `Batch.__exit__` on an exception inside the `with`.
- **I10 decodable history** — every row declares `schema_version` and
  `encoding`; verify the check constraints exist and the upcast path runs on
  every read path including the worker's reconstruction.

### 3.4 Candidate findings to confirm or refute (each needs evidence)

These are areas I, the implementer, could not fully certify. **Do not take
them as given — reproduce each with a probe or disprove it.** They are listed
in priority order.

- **R1 — `replace()` changed-set bug.** `Collection.replace` passes the
  *new* state as the "before" document, so the inline `Commit.changed` for a
  replace is always empty (verified: `frozenset()`), while the worker
  reconstructs the true diff. Probe:
  ```python
  from pydantic import BaseModel
  from eventic import App, Stream, Subscription
  from eventic.sql import SQLite
  from eventic.envelopes import Commit

  class Todo(BaseModel):
      text: str
      done: bool = False

  seen = []
  def handler(c: Commit[Todo, BaseModel]):
      seen.append(c)

  todos = Stream(Todo, name='todos')
  app = App(id='d', streams=[todos],
            subscriptions=[Subscription(id='i', stream=todos, handler=handler)])
  ev = app.bind(SQLite(':memory:'))
  t = ev[todos].create(Todo(text='a'))
  ev[todos].replace(t, Todo(text='b', done=True))
  print(seen[1].changed)   # bug: frozenset()
  ```
  Then confirm the durable worker reconstruction produces `{'text','done'}`
  for the same commit, violating the §10 field-for-field envelope-equality
  claim and 004/F10. The existing equality test covers only create/change.
- **R2 — four-way property missing the fifth leg.** The guide's property
  includes `digest(rebuilt head)`; the Hypothesis machine
  (`tests/property/test_four_way.py`) checks head, replay(log), history[-1],
  and the returned Revision — but never runs `admin.rebuild_heads`. The
  rebuild leg is only tested in `tests/conformance/test_admin.py` with
  hand-seeded data. Determine whether the property as written would catch a
  rebuild divergence.
- **R3 — "Time" conformance scenario absent.** The Phase 6 scenario table
  requires "committed_at is UTC, database-assigned, monotonic within a
  batch"; `scenarios.py` has no such scenario. Verify what actually happens:
  SQLite `CURRENT_TIMESTAMP` is second-precision, so two commits in one
  second produce equal (not strictly increasing) timestamps — check the
  "monotonic" claim, and whether `committed_at` can ever come from the
  application process.
- **R4 — Atomicity table rows missing.** "head upsert fails → no log row" is
  in the Phase 6 table but not in the suite. Find a way to make the head
  upsert fail and verify the transaction aborts with nothing written, or
  report the gap.
- **R5 — `head()` kind lie.** Trace every consumer of
  `StoredRevision.kind` from `head()` (the worker uses log rows, but verify
  no path builds a `Commit` from a head read).
- **R6 — reserved delta keys.** Write a delta-encoded stream whose state has
  top-level keys `set`, `del`, `base`, or `every`; check whether reads,
  replay, and verify stay byte-exact or silently corrupt.
- **R7 — Postgres null-vs-missing.** The `json_paths` scenarios distinguish
  missing from JSON null on SQLite via `json_type`; the Postgres path uses
  `@>` containment. Only CI exercises it. Statically determine whether
  `{"meta": null}` vs missing are distinguished on Postgres, and whether
  `@>` with a null value means what the suite asserts.
- **R8 — schema-check seeding order.** `check()` seeds missing ledger rows
  and reports clean; drift is only caught when the row already exists. Judge
  the "caught at deploy time" claim (see §3.1 item 1).
- **R9 — delta point-read on switched streams.** A stream configured
  snapshot reading an old delta row falls back to a two-query read; the
  "one round trip" (§9) and "at most K rows" (§5.2) claims — verify the
  bounds hold in the switch-mid-life test (`test_encoding_switch_mid_life`).
- **R10 — worker `run_forever`** has no signal handling or graceful
  shutdown; a deployed worker cannot be stopped cleanly. Judge severity.
- **R11 — `verify` memory bound.** `SqlAdmin.verify` does a second full
  `_stream_log` pass after the per-row pass; check the "bounded memory per
  chunk" claim in `docs/BENCHMARKS.md`.
- **R12 — search cursor ordering.** `search` pages ordered by
  `aggregate_id` (UUID), which is not temporal; with concurrent writers a
  later-created head can sort earlier. Verify whether a page boundary can
  skip or duplicate results under concurrent writes.
- **R13 — `make_upcaster` identity.** It returns a fresh class per call;
  check `Upcaster` equality/hash behavior if upcasters are compared or
  stored.
- **R14 — Stream vs Meta equality.** `Stream.__eq__` is name-based,
  `Meta.__eq__` is model-identity-based; check `App` equality and hash
  semantics across these (two apps with same-named streams but different
  models compare equal? is that intended?).

### 3.5 Conformance completeness audit

Compare the Phase 6 scenario table (CAS, Replay, Identity, Atomicity,
Batch, Reads, Head, Time, Intents, Errors) against `scenarios.py` group by
group. List every table row with no scenario. Same for the encoding
conformance table (§Phase 12) and the delivery table (§Phase 10).

### 3.6 Definition-of-done audit (CONCEPT.md §12)

Each of the 15 statements claims to be "a test, not a judgement". For each,
name the test that proves it and verify the test actually proves the
statement — not merely that it exists. Pay special attention to:
- item 5/6 (byte-exact rebuild, no orphan) — does `rebuild_heads` prove it
  under delta?
- item 9 (every row declares schema version and encoding) — constraints vs
  `model_construct` paths;
- item 11 (SQLite and Postgres pass one identical suite) — the Postgres run
  is CI-only;
- item 13 (installed wheel executes the documented path) — the smoke test
  installs into a venv but runs from the checkout.

### 3.7 Claim audit against docs

Every guarantee sentence in `README.md` and `docs/*.md` that you can falsify
with a probe, probe it:

- "every read is one indexed query" / complexity table in BENCHMARKS.md —
  count statements with the `before_cursor_execute` technique used in
  `tests/conformance/test_encodings.py`.
- "history, where, and verify are paged/chunked; nothing materializes an
  unbounded result" — find the one path that materializes everything
  (e.g. `SqlAdmin.list_intents`, `verify`'s second pass).
- "No command prints a connection URL or a payload" — fuzz the CLI with a
  URL containing credentials and a payload in an error.
- "a missing upcaster is a declaration error, never a read-time surprise" —
  can you craft a database where a read raises `IncompleteUpcasterChain`?
- "INSERT-only role can run the full write path" (Phase 9) — CI-only;
  statically verify no log UPDATE/DELETE exists in the write path.
- The four-way agreement, the race canary, the at-least-once duplicate
  delivery, and "exactly once appears nowhere" (the grep test scans
  `src/` + `docs/`; does it miss the CLI's `--help` strings or examples?).

### 3.8 Working rules (same as the guide's §0.1, plus the review tradition)

1. Reproduce before reporting. Every finding carries the command or probe
   that demonstrates it. A finding without a reproduction is a hypothesis —
   mark it as such.
2. Never weaken an assertion to make a test pass. If a test is wrong, delete
   it and say why.
3. No test is named after a finding. Name tests after behavior.
4. Real databases only — SQLite is fast enough; never mock the database.
   Use a file-backed SQLite (WAL) for anything concurrent; `:memory:` with
   `StaticPool` for single-threaded probes.
5. Fresh processes for anything that touches import-time behavior or
   installed artifacts.
6. Treat every warning as an error (`-W error`), including the
   Pydantic-2.11 deprecation warnings and unclosed-connection
   ResourceWarnings that the suite itself is currently suppressing — check
   which tests use `@pytest.mark.filterwarnings` or `# type: ignore` and
   whether the suppression hides a real defect.
7. The review artifacts live in `.scratch/projects/006-implementation-review/`
   (not shipped — the sdist excludes `.scratch`). Probes go in
   `probes/`; commit them. The archived 00{1..4} directories are read-only
   — do not modify them.

## 4. Deliverable

Write `.scratch/projects/006-implementation-review/REVIEW.md` in the format
of the 004 review:

1. **Header**: date, scope (the §0.2 gate results, baseline commit, branch),
   environment notes (which Postgres tests could not run).
2. **Verdict**: release-ready / not release-ready, with the strongest claims
   either confirmed or falsified. The 004 verdict format is the model:
   name the specific claims that are false under normal usage, not a list of
   nits.
3. **Numbered findings** `F1..FN`, each with: severity (blocker / major /
   minor), the invariant or spec section it violates, the reproduction
   (command or probe, verbatim), and the smallest fix direction. If a
   candidate R1–R14 is refuted, record the refutation evidence briefly — a
   review that says "I checked X and it holds because …" is worth as much as
   a finding.
4. **What is already strong** — the preservation list (as 004 did), so the
   next round does not re-litigate decisions that hold.
5. **Unverified items** — everything that could only be statically reviewed
   (Postgres dialect paths, the INSERT-only role, the no-checkout wheel run).

Then commit the review artifacts to `v1` (the scratch directory is evidence,
like 001–004). A review that finds nothing is a review that did not look;
a review that finds only nits is not thorough.

## 5. Exit criteria

You are done when:

- [ ] Every invariant I1–I10 has a probe attempt recorded (pass or fail).
- [ ] Every §12 definition-of-done item maps to a test that actually proves it.
- [ ] Every Phase 6/10/12 conformance table row has a scenario or a reported gap.
- [ ] Every candidate R1–R14 is confirmed, refuted, or marked unverifiable — with evidence.
- [ ] The §0.2 gate results are recorded verbatim.
- [ ] `REVIEW.md` exists in the 004 format with a clear verdict and severity-ranked findings.
- [ ] Probes and the review are committed to `v1`.
