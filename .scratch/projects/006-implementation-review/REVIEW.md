# Eventic 1.0 — Implementation Review

**Date:** 2026-08-05
**Scope:** the 1.0 implementation on branch `v1`, reviewed against
`005-redesign/{CONCEPT,ARCHITECTURE,IMPLEMENTATION_GUIDE}.md` and the shipped
`docs/`.
**Baseline commit:** `bfe241f` (`docs: 006 implementation-review kickoff prompt`),
tree clean apart from this review's own artifacts.
**Reviewer:** fourth review of this library (after 001, 003, 004).

## Environment

Reviewed inside a `devenv` shell added to the repo during this review
(`devenv.nix` / `devenv.yaml`, from the `template-py` genome at `v0.1.8`, applied
additively — `pyproject.toml`, `README.md` and `src/` were deliberately left
untouched so the artifact under review was not modified). Python pinned to
**3.13.14** to match the CI matrix leg; the template's unpinned default served
3.14.6, which would not have been a sound interpreter to review on.

The gate was run both in the pre-existing `.venv` (Python 3.13.13) and in the
devenv venv (3.13.14). **Results were identical.**

### §0.2 gate, verbatim

```console
$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
107 files already formatted

$ uv run basedpyright
0 errors, 0 warnings, 0 notes

$ uv run pytest -W error
210 passed, 5 skipped in 230.75s (0:03:50)
```

The `ruff format` count is 107 rather than the 81 a pre-review checkout reports;
the difference is 26 Markdown files added by the devenv template (ruff 0.16
formats Python code blocks inside Markdown). No source file changed. All five
skips are `tests/conformance/test_postgres.py` — no live Postgres was available,
so every Postgres path in this review is **statically reviewed only**.

---

## Verdict

**Not release-ready.**

The gate is green, the architecture is genuinely faithful to the design in most
places, and the pure core (canonicalisation, planning, hydration, encodings) is
the strongest layer this library has had. But two claims that the design calls
the spine are false under ordinary usage:

1. **"The log is the only truth; the head is derived from it" (I2) is false.**
   Replaying an already-committed revision *rewrites the head backwards*. A
   client that retries a commit — the exact operation the replay path exists to
   make safe — silently rewinds the aggregate to an older state, and every
   subsequent `get()` returns stale data until an operator runs `heads rebuild`.
   Reproduced end-to-end through the public API (F1).

2. **"Loud conflicts" (I7) is false on the production backend.** §4.3 step 1
   requires the CAS read to take a row lock. `for_update=False` is hardcoded at
   the only call site and `with_for_update()` is unreachable dead code. SQLite
   hides this behind `BEGIN IMMEDIATE`; Postgres has no equivalent —
   `Postgres._install_events` is `pass`. Two writers race, the loser hits the
   unique constraint, and the constraint violation surfaces as `StoreError`, not
   `RevisionConflict` (F2). A caller running the documented optimistic-retry
   loop does not retry.

The design's own bar was: *"Every release blocker in 004 is an instance of
computing the same thing twice (I3) or failing to make the writes atomic and
store-scoped (I8)."* F1 is a third thing the bar did not anticipate — writing the
derived projection from a **stale source row**. F2 is 004/F21's shape recurring:
a guarantee whose mechanism exists only on the dialect the tests actually run.

Both are small fixes. Neither is a design failure. But both falsify a headline
guarantee, and one of them corrupts reads silently.

A third result is worth stating plainly because it is about the review process
rather than the code: **the test that is supposed to make this library's root
cause impossible to write does not catch the root cause as it was historically
written** (F4). `test_no_module_level_mutable_binding` skips every name starting
with `_`; injecting `planning._CURRENT_STORE = {}` — the literal shape of
003/F8's `_ENGINE` and 004/F16's `_CURRENT` — leaves the suite green.

---

## Findings

### F1 — Replay detection rewinds the head to a superseded revision

**Severity:** blocker
**Violates:** I2, I3; `CONCEPT.md` §12 item 5; `ARCHITECTURE.md` §4.3 step 2

`SQLite._commit_one` (`src/eventic/sql/store.py:156-187`) reads the head row,
then looks for a log row at the target revision. If one exists and
`_is_identical` holds, it calls `_upsert_head_from_row(conn, existing, ...)`,
which upserts the head **from that row**. Nothing compares the existing row's
revision against the head's. `Dialect.upsert_head` is an unconditional
`ON CONFLICT DO UPDATE` (`src/eventic/sql/dialect.py:118-149`), so the head is
overwritten with the older revision's state, digest, and `revision_id`.

**Reproduction** — `probes/p02_replay_rewinds_head.py`:

```console
$ devenv shell -- uv run python .scratch/projects/006-implementation-review/probes/p02_replay_rewinds_head.py
after three commits:
  head revision : 2
  head state    : {'done': False, 'text': 'c'}

replay of revision 1 -> replayed = True
  head revision : 1
  head state    : {'done': False, 'text': 'b'}
  log latest    : 2 {'done': False, 'text': 'c'}

  ev[todos].get(id).state.text = 'b' (should be 'c')
```

`head.digest != log[2].digest` afterwards. Nothing raises; nothing logs. The log
itself is untouched (I1 holds), so `eventic heads rebuild` repairs it — but only
if someone knows to run it.

**Reachability.** The replay path exists precisely so an at-least-once caller may
re-send a commit. Any retry of a superseded revision triggers it: a client whose
ack was lost, a queue redelivery, a user double-submitting a form against a
`base` handle they still hold.

**Why the suite misses it.** The `REPLAY` group's only positive scenario
(`scenarios.py:121-145`) replays revision 1 while the head *is* revision 1, so
the upsert is a no-op. There is no scenario replaying a revision the head has
moved past.

**Smallest fix.** In the replay branch, only upsert the head when
`head_row is None or existing["revision"] > head_row["revision"]`; otherwise
return `replayed=True` and leave the head alone. Add a scenario: create, change,
change, replay revision 1, assert `head.revision == 2`.

---

### F2 — The CAS read takes no row lock; a lost race is `StoreError`, not `RevisionConflict`

**Severity:** blocker (Postgres path unverified — no live service)
**Violates:** `ARCHITECTURE.md` §4.3 step 1; I7

§4.3 step 1: *"Read the head row for `(stream, aggregate_id)` with row-level
locking"* and *"Constraint violation on `(stream, aggregate_id, revision)` also
maps to `RevisionConflict`."*

Neither holds:

- `src/eventic/sql/store.py:158` calls `st.select_head(..., for_update=False)`.
  That is the only CAS call site, and `for_update=True` is passed nowhere in the
  codebase — `statements.py:36-37`'s `with_for_update()` is unreachable.
- `Postgres._install_events` is `pass` (`store.py:700-701`). Default READ
  COMMITTED, no advisory lock, no `SELECT … FOR UPDATE`. Two writers with the
  same `expected_revision` both read the head, both pass the CAS, both INSERT.
- `commit()` wraps everything in `except Exception → StoreError("commit failed")`
  (`store.py:142-145`). There is no `IntegrityError` handling anywhere in the
  module, so the constraint backstop cannot produce `RevisionConflict`.

SQLite is unaffected in practice because `_install_events` issues
`BEGIN IMMEDIATE` on every transaction, serialising writers.

**Reproduction of the error-mapping half** (deterministic, on SQLite) —
`probes/p07_cas_race_mapping.py`:

```console
=== simulate the Postgres interleaving ===
  caller sees -> StoreError: commit failed  (cause: StatementError)
```

The locking half is static-only: it requires a live Postgres and two concurrent
sessions.

**Why the suite misses it.** `tests/conformance/test_race_canary.py` constructs
`SQLite(...)` only. The declarative suite has **zero** concurrency scenarios —
`tests/conformance/test_postgres.py:73-77` is named
`test_concurrent_drainers_scenario_active` and asserts `assert not names`, with
the comment *"concurrent drainer scenarios land in Phase 10"*. Phase 10 shipped.
That test is a stale placeholder asserting the absence of the coverage its name
claims, and it is one of the five skips that make Postgres coverage look larger
than it is.

**Smallest fix.** Pass `for_update=True` from `_commit_one` (SQLite ignores it;
Postgres honours it), and add
`except sqlalchemy.exc.IntegrityError → RevisionConflict` inside `commit()`
before the generic handler. Then run the race canary against both backends.

---

### F3 — `Collection.replace` computes `changed` against the wrong document

**Severity:** major (candidate R1 — **confirmed**)
**Violates:** `ARCHITECTURE.md` §2.2; `CONCEPT.md` §10; 004/F10

`Collection.replace` (`src/eventic/runtime.py:69-77`) passes the *new* state as
the `before` argument to `_commit_one`. `_changed` then diffs the new document
against itself, always yielding `frozenset()`. The worker reconstructs the true
diff from the log (`worker.py:119-129`), so the inline and durable envelopes for
the same commit disagree — the exact property §2.2 says is guaranteed
("field-for-field identical envelopes").

**Reproduction** — `probes/p01_replace_changed.py`:

```console
create changed : ['done', 'text']
replace changed: []
worker changed : ['done', 'text']
```

**Why the suite misses it.** The envelope-equality test covers `create` and
`change` only; the four-way property declares no subscriptions, so `changed` is
never asserted there.

**Smallest fix.** `return self._commit_one(request, base.state)` in `replace`, as
`change` already does. Extend the envelope-equality test to `replace`.

---

### F4 — The I4/R9 enforcement test cannot see a private module-level mutable

**Severity:** major
**Violates:** I4, I5, R9; `ARCHITECTURE.md` §9.1

`tests/architecture/test_no_global_state.py:29-30` skips every attribute whose
name starts with `_`. Module-level caches are conventionally private, and the
three globals that caused this library's entire redesign were named `_ENGINE`,
`_CURRENT`, and activation tokens (003/F8, 003/F10, 004/F07, 004/F16).

**Reproduction** — `probes/p06_enforcement_gaps.py`:

```console
  clean tree offenders: []
  after injecting planning._CURRENT_STORE = {} and _SEEN_AGGREGATES = set():
    offenders: []
  after injecting a PUBLIC planning.CACHE = {}: ['eventic.planning.CACHE']
```

The scan works for the shape it checks; it just excludes the shape that
historically occurred. It also cannot see mutable state held as a class
attribute, inside a module-level object, or behind a `Final` annotation.

**Smallest fix.** Drop the `_`-prefix exemption and allowlist the handful of
legitimate private module constants explicitly, or assert immutability
(`Mapping`/`frozenset`/tuple) rather than filtering by name.

---

### F5 — `verify` and `heads rebuild` materialise one full document per aggregate

**Severity:** major (candidate R11 — **confirmed, and it also applies to `rebuild_heads`**)
**Violates:** `docs/BENCHMARKS.md` complexity table; 004/F29

`_stream_log` (`src/eventic/sql/admin.py:52-69`) reads the log in chunks but
folds every row into `final: dict[(stream, aggregate_id) -> full document]`,
which is returned whole. `verify` calls it *after* its per-row pass
(`admin.py:209`), and `rebuild_heads` calls it too (`admin.py:144`). Peak memory
is `O(aggregates x document)`, independent of `chunk`.

`docs/BENCHMARKS.md` states: *"`verify` / `heads rebuild` | chunked log stream |
`O(total rows)`, **bounded memory per chunk**"* and *"nothing materializes an
unbounded result."* Both are false.

**Reproduction** — `probes/p05_time_atomicity_bounds.py`:

```console
  400 aggregates; verify peak KiB at chunk=10  : 217
  400 aggregates; verify peak KiB at chunk=400 : 543
  _stream_log(chunk=10) returned 400 fully-materialized documents
```

`SqlAdmin.list_intents` (`admin.py:227-236`) is docstringed *"Paged-listing
support"* and has no `limit`, `offset`, or cursor — it materialises the entire
intent table, which is the one table that grows without bound under a stalled
worker.

**Smallest fix.** Either make the claim honest (document the real bound), or
stream per-aggregate: the log is already ordered by
`(stream, aggregate_id, revision)`, so a document can be finalised and released
as soon as the aggregate key changes. Give `list_intents` a `limit`/cursor.

Note the paging query itself uses `OFFSET` (`statements.py:218-224`), which is
`O(n²)` over a large log independent of the memory issue.

---

### F6 — Three declaration error classes are defined, documented, and never raised

**Severity:** major
**Violates:** `ARCHITECTURE.md` §2.1 error table, §8

§2.1 maps each `App` check to a specific class. `App._validate`
(`src/eventic/app.py:95-124`) accumulates strings and raises a bare
`ConfigError` for all of them. `DuplicateId`, `UnknownStream`, and
`UnsupportedHandler` are raised **nowhere** in `src/`.

**Reproduction** — `probes/p03_declaration_contract.py`:

```console
  duplicate stream name         spec=DuplicateId        actual=ConfigError
  duplicate subscription id     spec=DuplicateId        actual=ConfigError
  subscription on uninstalled…  spec=UnknownStream      actual=ConfigError
  async handler                 spec=UnsupportedHandler actual=ConfigError
```

**Why the suite misses it.** `tests/unit/test_errors.py` asserts only that the
classes exist and subclass `ConfigError` (lines 50-52) — taxonomy, not behaviour.
`tests/unit/test_app.py` catches `ConfigError` throughout. No test could fail.

**Smallest fix.** Either raise the specific classes (collecting them and raising
the most specific common base when several kinds co-occur), or delete the three
classes and correct §2.1/§8. The current state is the worst of both: a
documented API surface that cannot be caught.

---

### F7 — All concurrency coverage is SQLite-only; the conformance suite has none

**Severity:** major
**Violates:** `CONCEPT.md` §12 item 11; the "Identity and concurrency" row of the
mandatory validation matrix

The matrix requires *"8+ threads racing one `(stream, id, revision)` → exactly 1
winner (the 0.2 canary that must never regress)"*. It is tested only against
SQLite, under both encodings (`test_race_canary.py:92-113`). Postgres runs
`run_all(...)` over the 27 declarative scenarios, none of which are concurrent.

§12 item 11 — *"SQLite and PostgreSQL pass one identical store conformance
suite"* — is literally true and materially misleading: the identical suite does
not exercise the property that distinguishes the two backends' concurrency
models, which is the reason two backends exist (§10.6, 004/F23).

This is what makes F2 survivable to green CI.

**Smallest fix.** Parameterise the canary over a store factory and run it against
both backends in CI; add capability-gated concurrent-drainer scenarios and delete
`test_concurrent_drainers_scenario_active`.

---

### F8 — The four-way property is three-way, and snapshot-only

**Severity:** major (candidate R2 — **confirmed as a coverage gap**)
**Violates:** `IMPLEMENTATION_GUIDE.md` Phase 8; `ARCHITECTURE.md` §11

The guide's property is five-legged: `head == replay(log) == history[-1] ==
returned == rebuilt head`. `FourWayMachine._check`
(`tests/property/test_four_way.py:57-67`) checks the first four. `rebuild_heads`
is never called, so a rebuild divergence cannot be caught by the artifact
ARCHITECTURE §11 calls *"the highest-leverage artifact in the suite"*.

Separately, the machine constructs `SQLite(":memory:")` with no `encodings`
(line 51), so all 300 examples run under `snapshot/1`. `delta/1` — the encoding
with a correctness bug in every prior version (003/F4, 003/F17, 004/F02,
004/F11) — has no property coverage at all; it is covered only by hand-written
scenarios.

**The behaviour itself holds.** I probed the missing leg directly
(`probes/p04_delta.py`): 61 revisions under `delta/1` with `every=20`, rebuilt
with `chunk=10` so the fold crosses chunk and checkpoint boundaries — the head
digest is byte-identical before and after, and an injected orphan head is
removed. So this is a coverage finding, not a live defect.

**Smallest fix.** Add `rebuild_heads` to `_check`, and parameterise the machine
over `{snapshot/1, delta/1}`.

---

### F9 — `Stream`, `Meta`, and therefore `App` equality ignore model and version

**Severity:** minor (candidate R14 — **confirmed, broader than described**)

`Stream.__eq__` is name-only (documented, deliberate). `Meta.__eq__` is
`self.model is other.model` (`src/eventic/meta.py:50-53`) — it ignores `version`
and `upcasters` entirely, and is **not** documented as deliberate. `App` is a
frozen Pydantic model, so its equality delegates to both.

**Reproduction** — `probes/p03_declaration_contract.py`:

```console
  Stream(Todo,'todos') == Stream(Other,'todos')      -> True
  Meta(Todo,version=1) == Meta(Todo,version=2)       -> True
  hash equal                                         -> True
  App(streams=[Todo],meta=v1) == App([Other],meta=v2)-> True
```

Two apps declaring different state models *and* different metadata versions
compare equal and hash equal. Phase 4's test *"two identical declarations compare
equal"* passes; the converse is untested and false.

**Smallest fix.** Include `version` in `Meta.__eq__`/`__hash__`. If `Stream`'s
name-only equality is intentional (it is defensible — the name is the durable
identity), document that `App` equality is therefore identity-of-declaration, not
equivalence-of-behaviour.

---

### F10 — `committed_at` is second-precision on SQLite; the Time scenario does not exist

**Severity:** minor (candidate R3 — **confirmed**)
**Violates:** `IMPLEMENTATION_GUIDE.md` Phase 6 "Time" row

`_now()` issues `SELECT now()` once per transaction, which SQLAlchemy compiles to
`CURRENT_TIMESTAMP` on SQLite — whole seconds. Every request in a batch shares
one reading, and sequential commits within a second are indistinguishable.

**Reproduction** — `probes/p05_time_atomicity_bounds.py`:

```console
  three sequential commits, committed_at:
    2026-08-05T19:34:11+00:00  tz=UTC
    2026-08-05T19:34:11+00:00  tz=UTC
    2026-08-05T19:34:11+00:00  tz=UTC
  strictly increasing? False
  distinct committed_at within one batch: 1 (batch of 2)
```

UTC ✓ and database-assigned ✓; "monotonic" holds only non-strictly. The Phase 6
table's **Time** row has no scenario — the group is named `HEAD_TIME`
(`scenarios.py:448`) but contains only the Head scenario, which hides the gap.

**Smallest fix.** State the real guarantee ("non-decreasing; not a sort key")
in `docs/`, and add a Time scenario asserting exactly that.

---

### F11 — `Worker.run_forever` has no graceful shutdown

**Severity:** minor (candidate R10 — **confirmed**)

```python
def run_forever(self, *, poll: timedelta = timedelta(seconds=1)) -> None:
    while True:
        self.drain_once()
        import time
        time.sleep(poll.total_seconds())
```

No signal handling, no stop flag, no return path. `eventic worker` under a
process supervisor can only be killed. Because `drain_once` claims a batch,
leases it, then delivers, a SIGTERM mid-batch leaves intents `leased` until the
lease expires — correct (at-least-once absorbs it) but it delays redelivery by
the full lease on every deploy.

**Smallest fix.** A `threading.Event` stop flag checked each iteration and
`signal.signal(SIGTERM, ...)` installed by the CLI, not the library.

---

### F12 — `schema check` writes to the database and defines its own baseline

**Severity:** minor (candidate R8 — **largely refuted; residual issue recorded**)

`SqlAdmin.check` (`admin.py:114-129`) inserts the declared fingerprint when the
ledger row is absent and reports `ok=True`.

**Reproduction** — `probes/p08_paging_and_schema_check.py`:

```console
  check #1 on an empty database: drift=False
  check #2 with a changed model, same schema_version: drift=True
```

**The claim mostly holds.** Because `commit` upserts the fingerprint on every
write, any database that has been written to already has a baseline, and drift
*is* caught at deploy time. The residual problems are narrower than R8 supposed:
a command named `check` mutates production, and on a never-written database the
first check defines rather than verifies.

**Smallest fix.** Make `check` read-only and report "no baseline recorded" as a
distinct state from "clean".

---

### F13 — `changed_keys` cannot report a removed top-level key

**Severity:** minor

`changed_keys` (`planning.py:117-121`) iterates `after` only, so a key present in
`before` and absent from `after` is invisible. Unreachable for a fixed model, but
reachable for `model_config = {"extra": "allow"}` streams and for `dict[str, Any]`
fields at the top level.

**Reproduction:** `changed_keys({'a': 1, 'removed': 2}, {'a': 1}) == frozenset()`.

Note `delta/1` handles removal correctly via explicit tombstones — this is a
`Commit.changed` defect only, not a storage one, so 003/F4 has not recurred.

**Smallest fix.** `frozenset(before.keys() ^ after.keys()) | {k for k in after if k in before and before[k] != after[k]}`.

---

### F14 — The `"exactly once"` grep does not scan `README.md`

**Severity:** minor

`tests/architecture/test_delivery_contract.py:13-16` scans `src/**/*.py` and
`docs/**/*.md`. `README.md` lives at the repo root and is not scanned — it is the
primary documentation and the only file whose code blocks are executed as
doctests. The claim holds today (I verified `README.md` does not contain the
phrase); the guard does not.

The CLI's `--help` strings live under `src/eventic/cli/`, so those *are* covered
— the §3.7 concern about them is refuted.

**Smallest fix.** Add `ROOT.glob("*.md")` to `_source_files()`.

---

### F15 — `statements.claim_intents` is dead and would not execute

**Severity:** minor

`claim_intents` (`statements.py:100-132`) builds an `update(intents)` whose
`.returning()` selects `revisions.c.stream`, `revisions.c.aggregate_id`,
`revisions.c.revision` — columns of a table the statement does not join. It has
no call site; `SQLite.claim` uses `dialect.claim_select` plus
`claim_mark_leased`. It is a builder that cannot work, kept alive by never being
called.

**Smallest fix.** Delete it.

---

### F16 — A row written by a newer `schema_version` is read silently by an older declaration

**Severity:** minor

`_upcast_tree` (`hydration.py:57-58`) returns the tree unchanged when
`from_version >= to_version`. A v2 row read by a process declaring
`schema_version=1` is validated against the v1 model with no downgrade path and
no warning; extra keys are dropped by Pydantic's default `extra="ignore"`, and
the resulting `Revision` looks valid.

Phase 11's rolling-upgrade test covers *"a v1 writer and a v2 reader"*. The
dangerous direction — a v2 writer and a not-yet-deployed v1 reader, which is what
every rolling deploy produces for a few minutes — is untested.

**Smallest fix.** Raise `UndecodableRevision` when
`stored.schema_version > stream.schema_version`, naming both versions.

---

## Refuted candidates

Recorded with evidence, so the next round does not re-litigate them.

| # | Candidate | Verdict |
|---|---|---|
| **R4** | head-upsert failure leaves a log row | **Refuted.** Forcing `Dialect.upsert_head` to raise aborts the transaction; the log row and head are both absent afterwards (`p05`). I8 holds at this boundary. The Phase 6 table row still has no scenario — coverage gap only. |
| **R5** | `head()`'s fabricated `kind` is observable | **Refuted.** `_head_row_to_stored` returns `kind="change"` always, but `hydrate` ignores `kind`, `Revision` has no `kind` field, and every `Commit` is built from `request.kind` (runtime) or a log row (`worker.py:114`). Grep of all `.kind` consumers shows no path from a head read to a `Commit`. Remains a wart in the `Store.head` contract for third-party store authors. |
| **R6** | reserved delta keys (`set`/`del`/`base`/`every`) corrupt reads | **Refuted.** The delta envelope *nests* the document under `set` rather than merging, so user keys cannot collide with envelope keys. Probed with a model whose fields are literally `set`, `base`, `every` across 8 revisions with `every=5`: all digests byte-exact (`p04`). |
| **R9** | delta point-read exceeds its bound | **Refuted.** `revision(47)` with `K=20` issues one window `SELECT` spanning 8 rows (`p04`). The `≤ K+1` contract holds. |
| **R12** | search cursor can skip or duplicate under concurrent writes | **Refuted.** Keyset paging on `aggregate_id` — an immutable primary-key column — is stable. Paged 10 aggregates at `limit=3` while inserting a new lowest-UUID aggregate mid-run: 0 duplicates, 0 skips of pre-existing rows (`p08`). A row created after paging begins and sorting before the cursor is missed, which is inherent to keyset paging; the ordering being non-temporal makes *which* rows are missed unpredictable, but it does not corrupt a page boundary. |
| **R13** | `make_upcaster` identity | **Refuted as a defect.** It does return a fresh class per call, so two upcasters are never equal (`p03`). Nothing in the library compares, hashes, or stores upcasters — `Stream.__hash__` is name-based, `Meta.__hash__` is model-based. Latent only. |
| **R7** | Postgres null-vs-missing | **Unverifiable here** — see Unverified items. Static reading of `Dialect.path_equals` (`dialect.py:114-116`) suggests the suite's assertion is sound: `@>` containment of `{"meta": null}` matches a document with an explicit JSON null and does not match a document lacking the key, because `@>` requires the key to be present. Not executed. |

---

## What is already strong

Preserved deliberately, so the next round does not re-open it.

- **The pure core is genuinely pure.** `planning`, `hydration`, `canonical`,
  `evolution`, and `retry` take values and return values. The async-readiness
  audit's claim that a port touches nothing above `protocols.py` looks correct
  from reading the call graph.
- **`I3`'s mechanism works.** The head really is derived by decoding the row just
  encoded, and the digest assertion (`store.py:242-251`) aborts the transaction
  on an encoder bug. 004/F01 has not recurred. F1 is a *different* bug — a stale
  source row, not a second computation.
- **`delta/1` is correct.** Explicit tombstones, checkpoint at revision 0 and
  every `K`, bounded-window reads, chain validation on decode, and byte-exact
  rebuild across chunk boundaries. 003/F4, 003/F17, and 004/F11 are all closed —
  I probed each directly.
- **I1 holds structurally.** No `UPDATE` or `DELETE` against `eventic_revision`
  exists anywhere in `src/`. `heads rebuild` deletes only from `eventic_head`;
  `settle` deletes only from `eventic_intent`.
- **I5 holds.** The only `store.commit` call sites outside `sql/` are
  `runtime.py:145` and `runtime.py:209` — both inside `Collection`/`Batch`. There
  is no ambient store, no `ContextVar`, no `save()`.
- **Aggregate identity includes the stream.** 004/F03 is structurally closed;
  the same UUID in two streams is two aggregates, with a scenario proving it.
- **`SecretStr` is rejected at `Stream` construction** rather than silently
  serialising `"**********"` into an append-only log — the Phase 2 trap was taken
  seriously.
- **`Batch` genuinely has no reads.** The attribute does not exist, so the
  read-your-writes question is unaskable as §10.3 intended.
- **Driver exceptions do not escape.** Every public store method translates at
  the boundary. (F2 is that the translation is too coarse, not that it is
  missing.)
- **Constraint matrix is real.** Negative revision, invalid kind, and empty
  stream are all rejected by database check constraints under direct SQL.

---

## Unverified items

Everything below could only be read, not run.

1. **All five Postgres tests** (`test_postgres.py`) — no live service. This
   covers the store conformance run on Postgres, schema parity between
   `create_all` and `alembic upgrade head`, `alembic check` cleanliness, and the
   JSONB replay test. F2's locking half sits entirely here.
2. **The `INSERT`/`SELECT`-only role** (Phase 9). Statically: no `UPDATE` or
   `DELETE` against `eventic_revision` exists, so the write path should survive
   the grant. Never executed.
3. **`@>` containment semantics for JSON null vs missing** on Postgres (R7).
4. **`concurrent_drainers=True`** on Postgres — declared in
   `POSTGRES_CAPABILITIES` but exercised by no scenario on any backend.
5. **The clean-wheel smoke test's CI claim.** `tests/integration/wheel/` builds
   and installs into a fresh venv, but runs from the project checkout; the
   "runner with no project checkout" claim in Phase 14 is only statically true.
6. **Benchmark numbers** in `docs/BENCHMARKS.md` — regenerated on a different
   machine; not reproduced. The *complexity contracts* were checked and one is
   false (F5).
7. **Clock-skew behaviour of leases.** `SQLite.claim` uses
   `datetime.now(UTC)` — the application clock, not the database clock
   (`store.py:493`, with a comment explaining why). Correct for SQLite's
   second-precision `CURRENT_TIMESTAMP`; on Postgres with multiple worker hosts,
   lease expiry is subject to host clock skew. Not tested.

---

## Conformance-table gaps (§3.5)

Phase 6's table versus `scenarios.py` (27 scenarios total):

| Group | Table rows | Present | Missing |
|---|---|---|---|
| CAS | 7 | 7 | — |
| Replay | 4 | 4 | replay of a **superseded** revision (F1) |
| Identity | 1 | 1 | — |
| Atomicity | 3 | 2 | *head upsert fails → no log row* (behaviour verified by probe, F5/R4) |
| Batch | 2 | 1 | *ordering preserved in results* |
| Reads | 6 | 5 | — |
| Head | 1 | 1 | — |
| **Time** | 1 | **0** | the whole row (F10) — group is named `HEAD_TIME` but holds only the Head scenario |
| Intents | 5 | 5 | *concurrent drainers (capability-gated)* — explicitly asserted absent (F7) |
| Errors | 2 | 1 | *every failure raises from the public tree* is only partially covered; F2 is a live counter-example |

---

## Definition-of-done (§12) — items needing attention

Items 1–4, 7, 8, 10, 12, 14 map to tests that do prove them. The rest:

| # | Statement | Status |
|---|---|---|
| 5 | Heads byte-exactly rebuildable | **False in the presence of F1.** The rebuild itself is byte-exact (probed under delta); the head can be wrong *before* one runs. |
| 6 | Rebuild changes no digest, leaves no orphan | Holds (probed, `p04`) — but proven only by `test_admin.py` on hand-seeded data, not by the property (F8). |
| 9 | Every row declares schema version and encoding | Holds; check constraints exist and no `model_construct` path bypasses them. |
| 11 | SQLite and Postgres pass one identical suite | Literally true, materially misleading (F7). |
| 13 | Installed wheel executes the documented path | Runs from the checkout (unverified item 5). |
| 15 | Async audit enforced; "exactly once" nowhere | Audit is real; the grep's scope excludes `README.md` (F14). |

---

## Recommended order of work

1. **F1** — one condition in the replay branch, plus the missing scenario.
2. **F2** — `for_update=True` and an `IntegrityError` arm; then run the canary
   on both backends (F7).
3. **F3** — one-word fix, plus extending the envelope-equality test to `replace`.
4. **F4** — the enforcement test cannot be left blind to the bug class this
   library exists to have deleted.
5. **F5, F6** — a doc correction and an API decision respectively; both cheap.
6. **F8** — add the fifth leg and the delta parameterisation before the next
   release, not after.

F1 and F2 are the release blockers. Everything else can ship behind an honest
changelog.
