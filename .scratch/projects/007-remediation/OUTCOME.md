# 007 Remediation — Outcome

Closes the sixteen findings of `006-implementation-review/REVIEW.md` (two
release blockers). One commit per phase; every finding has a test that failed
before the fix and passes after. The gate — `ruff check`, `ruff format
--check`, `basedpyright`, `pytest -W error` — is green at the end, with a live
Postgres: **243 passed, 0 skipped**.

## The two falsified claims, confirmed true

The 006 verdict rested on two headline claims being false. Each is now true,
with the command that shows it:

1. **"The log is the only truth; the head is derived from it" (I2).**
   Replaying a superseded revision used to rewind the head. Now:
   ```console
   $ devenv shell -- uv run python .scratch/projects/006-implementation-review/probes/p02_replay_rewinds_head.py
   replay of revision 1 -> replayed = True
   head revision : 2
   OK: head stayed at revision 2; head.digest == log[2].digest -> I2 holds.
   ```
   Asserted as a conformance scenario on both backends (`replay of a
   superseded revision leaves the head alone`).

2. **"Loud conflicts" (I7).** A lost race used to surface as `StoreError` on
   Postgres. Now the race canary runs against both backends and the loser is
   always `RevisionConflict`:
   ```console
   $ EVENTIC_PG_URL=... devenv shell -- uv run pytest -W error \
       tests/conformance/test_race_canary.py tests/conformance/test_postgres.py -v
   test_eight_threads_race_one_revision_one_winner[False-postgres] PASSED
   ... 11 passed, 0 skipped
   ```
   The canary asserts `not other` — the exact assertion that would catch a
   `StoreError` regression.

## Finding → commit → proof

| # | Severity | Commit | Test that proves it |
|---|---|---|---|
| F1 | blocker | `c7c7768` fix(store): replay of a superseded revision leaves the head alone | `tests/conformance/test_replay.py::test_replay_of_superseded_revision_leaves_head_alone`; scenario `replay of a superseded revision leaves the head alone` (both backends); probe p02 |
| F2 | blocker | `96f8134` fix(store): lock the CAS, map the constraint backstop to RevisionConflict | race canary on SQLite **and** Postgres under both encodings (`test_eight_threads_race_one_revision_one_winner[*-postgres]`); scenarios `same-expected-revision race has exactly one winner` and `concurrent create of the same aggregate has exactly one winner` (the create race has no head row to lock — it proves the constraint backstop); probe p07 |
| F3 | major | `535fd20` fix(runtime): replace diffs against the previous state | `test_worker.py::test_replace_reports_changed_for_replaced_keys`, `test_batch_replace_reports_changed_for_replaced_keys`; envelope-equality test extended to create+change+replace; probe p01 |
| F4 | major | `156fa47` fix(architecture): the purity test sees private module-level mutables | `test_no_global_state.py::test_scan_catches_an_injected_private_module_mutable` (injects `planning._CURRENT_STORE`); probe p06 |
| F5 | major | `91c2315` fix(admin): stream the log fold per aggregate; page list_intents | `test_admin.py::test_verify_memory_bounded_per_chunk_not_per_aggregate`, `test_list_intents_respects_limit_and_cursor_roundtrips`; `docs/BENCHMARKS.md` states the real bound; probe p05 |
| F6 | major | `0a04130` fix(app): raise the §2.1 declaration error classes | `test_errors.py` behaviour tests (`test_duplicate_id_raised_*`, `test_unknown_stream_raised_*`, `test_unsupported_handler_raised_*`, mixed→base, single-class→joined); probe p03 taxonomy section (spec == actual, 4/4) |
| F7 | major | `96f8134` (with F2) | canary parameterised over a store factory, runs on both backends; capability-gated `concurrent drainers claim each intent without overlap`; stale `test_concurrent_drainers_scenario_active` deleted |
| F8 | major | `d41fa55` test(property): five legs under both encodings | `test_four_way_agreement[snapshot/1]` and `[delta/1]` with the `rebuild_heads` fifth leg active |
| F9 | minor | `f28bd04` fix(meta): Meta equality and hash include version | `test_meta_equality_includes_version`, `test_app_equality_includes_meta_version` |
| F10 | minor | `0a32dd3` feat(conformance): Time scenario | scenario `committed_at is UTC, non-decreasing, and equal within a batch` (both backends); I11 in `docs/INVARIANTS.md` |
| F11 | minor | `8953508` fix(worker): graceful shutdown via a stop flag | `test_run_forever_stops_via_stop_flag`; `test_cli.py::test_worker_stops_gracefully_on_sigterm` (real SIGTERM, exit 0) |
| F12 | minor | `22bbb79` fix(admin): schema check is read-only | `test_schema_check_seeds_missing_ledger` (ledger count stays 0); `test_cli.py::test_schema_upgrade_and_check` (no-baseline → exit 0, drift → exit 3) |
| F13 | minor | `c4069fa` fix(planning): changed_keys reports a removed top-level key | `test_changed_keys_reports_a_removed_key` (direct + extra="allow" end-to-end) |
| F14 | minor | `f11fc02` fix(test): the "exactly once" grep scans README.md too | `test_delivery_contract.py::test_exactly_once_appears_nowhere` (now scans root `*.md`, symlink-deduped) |
| F15 | minor | `5f23b44` chore(sql): delete dead broken claim_intents builder | deleted code; suite green |
| F16 | minor | `4aebc68` fix(hydration): a newer stored schema_version is undecodable | `test_v2_writer_and_v1_reader_raises` (head read and exact-revision read) |

## Things found and fixed beyond the sixteen

These are the reason the review insisted on a live Postgres (§0.4) and were
invisible behind the five skips:

- **The Postgres conformance factory shared one database across all 27
  scenarios** (fixed UUIDs collided — 13/27 failed with "row exists with
  different content"). SQLite got a fresh file per scenario; the Postgres
  factory now drops and recreates the schema per scenario.
  `3e3ea57 test(postgres): isolate scenarios per fresh database`.
- **`test_schema_parity_create_all_vs_alembic` compared `create_all` (4
  tables, no `alembic_version`) against `alembic upgrade head` (4 +
  `alembic_version`) — unequal by construction**, and worse when a leaked
  `alembic_version` stamp made the upgrade a no-op. Fixed in `3e3ea57`.
- **`eventic.encodings._ENCODING_INSTANCES`, a module-level backing dict for
  the encoding registry**, was hiding behind the `_`-prefixed exemption F4
  removed. The registry is now a `MappingProxyType` over an inline literal.
- **`test_changed_keys_ignores_missing_before_key` asserted the F13 bug** (a
  removed key invisible). Corrected to assert both directions, with the
  reasoning in the test and the commit message — no assertion was weakened.
- **A leaked `:memory:` store in the F13 test** caused a nondeterministic
  unraisable-ResourceWarning that failed `-W error`; closed (`0eee4f1`).
- **`test_list_intents` used `base.replace(second=base.second + i)`** to spread
  seven intents across distinct timestamps — when the test ran with a clock
  second ≥ 54, `second + i` overflowed and raised
  `ValueError: second must be in 0..59`, skipping `store.close()` and leaking
  the SQLite pool connection, which then surfaced as a
  `PytestUnraisableExceptionWarning` on whichever test ran at the GC — the
  intermittent `-W error` failures that dogged the final verification. Fixed
  with `base + timedelta(seconds=i)` (and the same pattern in probe p05);
  verified with 10 consecutive `test_admin` runs and 3 consecutive full runs,
  all green.

## Decisions recorded

- **F6, Option A** (raise the specific classes) — the §2.1 table is now
  honest; a single distinct fault raises as its class, heterogeneous faults
  raise the common `ConfigError` with every message, preserving the
  "reported together" requirement.
- **F5, Option A** (make the memory claim true) — `_stream_log` is now a
  callback fold that finalises each aggregate's document when its key
  changes; peak memory is one in-flight document plus one chunk of rows, plus
  O(aggregates) key bookkeeping for rebuild's orphan check. `BENCHMARKS.md`
  states the residual bound precisely rather than overclaiming.
- **F10** — `committed_at` precision is documented as the guarantee (UTC,
  database-assigned, non-decreasing, not a sort key) rather than pretended
  strict.
- **CONCEPT.md §12 item 13** (installed wheel runs from a checkout) —
  unchanged, matching 006's unverified item 5; noted in `COVERAGE.md`, not
  re-litigated.
- **`test_four_way.py`'s pre-existing `ignore::ResourceWarning`** —
  retained, not new; the guide forbids *new* `filterwarnings`.
