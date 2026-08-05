# Conformance-table coverage (Phase 9 close-out)

Every row of the `IMPLEMENTATION_GUIDE.md` Phase 6, Phase 10 and Phase 12
tables, mapped to the scenario or test that proves it. Scenarios live in
`src/eventic/testing/conformance/scenarios.py` and run on both backends via
the SQLite and Postgres conformance tests. Tests are named files under
`tests/`.

Capability gates: `outbox` (both backends), `json_paths` (both backends),
`concurrent_drainers` (Postgres only). A gated scenario skips — reported as a
skip, never as a pass — on a store lacking the capability.

## Phase 6 — the store contract

| Table row | Proved by |
|---|---|
| CAS: create on empty aggregate | `create on empty aggregate` |
| CAS: create when head exists → conflict | `create when head exists conflicts` |
| CAS: change with correct expected revision | `change with correct expected revision` |
| CAS: change with stale expected revision → conflict | `change with stale expected revision conflicts` |
| CAS: change with ahead expected revision → conflict | `change with ahead expected revision conflicts` |
| CAS: change with negative expected revision → conflict | `change with negative expected revision conflicts` |
| CAS: change on a nonexistent aggregate → conflict | `change on nonexistent aggregate conflicts` |
| Replay: byte-identical replay → `replayed=True`, one row | `byte-identical replay is a silent no-op, one row` |
| Replay: same key, different digest → conflict | `replay with different digest conflicts` |
| Replay: same key, different meta → conflict | `replay with different meta conflicts` |
| Replay: same key, different `schema_version` → conflict | `replay with different schema version conflicts` |
| Replay: superseded replay leaves the head alone (F1) | `replay of a superseded revision leaves the head alone` + `tests/conformance/test_replay.py` |
| Identity: same UUID in two streams | `same aggregate UUID in two streams is two aggregates` |
| Atomicity: intent insert fails → no log row, no head row | `invalid intent aborts the whole commit` |
| Atomicity: head upsert fails → no log row | `tests/conformance/test_store_contract.py::test_head_upsert_failure_leaves_no_log_row` (the scenario DSL cannot inject a store failure, so this lives next to the suite — comment in the test says so; behaviour first probed by `probes/p05`) |
| Atomicity: batch of 3 with the 2nd conflicting → nothing written | `batch with a mid-batch conflict writes nothing` |
| Batch: two writes to the same aggregate chain correctly | `two writes to the same aggregate chain in one batch` |
| Batch: ordering preserved in results | `batch results preserve request order` (the runner asserts `results[i]` matches `commits[i]` positionally) |
| Reads: head after N writes; exact revision at every n | `head, exact revisions, and history after several writes` |
| Reads: `history` paging with cursors | `history paging with cursors` |
| Reads: `history` on a missing aggregate | `history on a missing aggregate is empty` |
| Reads: `where` equality on top-level and dotted paths | `search equality on top-level and dotted paths` |
| Reads: missing path vs explicit JSON null are distinct | `missing path and explicit JSON null are distinct` |
| Head: head digest equals the log digest at every revision | `head digest equals log digest at every revision` |
| Time: `committed_at` is UTC, database-assigned, non-decreasing within a batch, equal within one commit (F10) | `committed_at is UTC, non-decreasing, and equal within a batch` |
| Intents: staged in the same transaction | `invalid intent aborts the whole commit` (a bad intent aborts the whole commit) |
| Intents: one function under two subscriptions produces two rows | `one function under two subscriptions produces two intent rows` + `tests/conformance/test_worker.py::test_one_function_two_subscriptions_two_deliveries` |
| Intents: claim / lease / ack | `claim, deliver, ack deletes the intent` |
| Intents: expired lease reclaimable | `expired lease is reclaimable` |
| Intents: concurrent drainers (capability-gated, F7) | `concurrent drainers claim each intent without overlap` |
| Errors: every failure raises from the public tree | `constraint violations surface as StoreError, never a driver exception` + `test_errors_all_public` (every `expect_error` in every scenario resolves to an `EventicError` subclass) + the Race step's `race produced non-Conflict outcomes` assertion (a lost race must be `RevisionConflict`, never `StoreError` — F2's live counter-example, now a standing assertion) |
| Errors: no driver exception escapes | the runner treats any non-`EventicError` raised by a step as a failure (`driver/foreign exception escaped`) |

## Phase 10 — delivery

| Table row | Proved by |
|---|---|
| claim / lease / ack | `claim, deliver, ack deletes the intent`; `test_deliver_and_ack` |
| crash after claim → lease expiry reclaims | `expired lease is reclaimable`; `test_crash_after_side_effect_duplicates_delivery` |
| crash after side effect → duplicate delivery (at-least-once proof) | `test_crash_after_side_effect_duplicates_delivery` |
| ack failure → lease expires, redelivered | `test_crash_after_side_effect_duplicates_delivery` (AckLost store) |
| retry with backoff | `retry makes the intent available again after backoff`; `test_retry_then_deliver` |
| retry exhaustion → dead | `dead-lettered intent is not claimable`; `test_retry_exhaustion_dead_letters` |
| redrive → pending | `tests/conformance/test_cli.py` (`intents redrive`) + `SqlAdmin.redrive` |
| concurrent drainers deliver each intent once (Postgres); capability declared False on SQLite | `concurrent drainers claim each intent without overlap` (gated); SQLite declares `concurrent_drainers=False` in `SQLITE_CAPABILITIES`, so the scenario skips there |
| one function, two subscriptions, two deliveries, no unique-constraint violation | `one function under two subscriptions produces two intent rows` + `test_one_function_two_subscriptions_two_deliveries` |
| inline and durable envelopes field-for-field equal, incl. `committed_at` and `changed` | `test_inline_and_durable_envelopes_identical`, `test_replace_reports_changed_for_replaced_keys` (F3) |
| handler module unimportable → worker app load fails non-zero | `test_cli.py::test_load_failure_is_nonzero_and_clear` |
| no credential / URL / payload in `last_error` or log lines | `test_last_error_redacted_no_credentials` |
| graceful stop on SIGTERM/SIGINT (F11) | `test_run_forever_stops_via_stop_flag`, `test_cli.py::test_worker_stops_gracefully_on_sigterm` |
| "exactly once" appears nowhere | `tests/architecture/test_delivery_contract.py` (scans `src`, `docs`, and root `*.md` — F14) |

## Phase 12 — `delta/1` and `eventic verify`

| Table row | Proved by |
|---|---|
| encoding conformance: digest equality at every revision (snapshot/1 and delta/1) | `tests/conformance/test_encodings.py::test_digest_equality_at_every_revision`; property: `test_four_way_agreement[snapshot/1]` / `[delta/1]` (F8) |
| checkpoint rows are full snapshots | `test_delta_checkpoint_rows_are_snapshots` |
| field removal round-trips (tombstones — 003/F4) | `test_delta_field_removal_round_trips` |
| corrupted row → mismatch/raise | `test_delta_corrupted_row_raises_or_mismatch`; `test_verify_detects_corruption` |
| missing checkpoint → `UndecodableRevision` | `test_delta_missing_checkpoint_raises` |
| point read at revision n touches ≤ K+1 rows | `test_point_read_touches_bounded_rows` (statement counting) |
| stream switching encodings mid-life stays readable | `test_encoding_switch_mid_life_leaves_history_readable` |
| `eventic verify` clean under delta | `test_verify_clean_under_delta`; `test_cli.py::test_verify_and_heads_rebuild` |
| rebuild is byte-exact, no orphans (I2 / §12 items 5-6) | `test_admin.py::test_rebuild_heads_byte_exact`, `test_rebuild_removes_orphan_heads`; property fifth leg (F8) |
| verify peak memory bounded per chunk (F5) | `test_admin.py::test_verify_memory_bounded_per_chunk_not_per_aggregate` |

## CONCEPT.md §12 definition-of-done re-check

| # | Item | Status after this remediation |
|---|---|---|
| 5 | Heads byte-exactly rebuildable; no digest changes, no orphan | Fixed (F1) and enforced: superseded-replay scenario, rebuild byte-exact tests, four-way fifth leg (F8) |
| 6 | Commit writes log row + head row + every intent, or none | Atomicity scenarios + `test_head_upsert_failure_leaves_no_log_row` |
| 9 | Every row declares schema version and encoding | Check constraints (`ck_schema_version`, `ck_encoding`); undecodable newer version now raises (F16) |
| 11 | SQLite and Postgres pass one identical suite | Now real: Postgres harness isolates scenarios per fresh schema; concurrency scenarios run on both backends (F2, F7) |
| 13 | Installed wheel executes the documented path | `tests/integration/wheel/` — still runs from the checkout (unverified item 5 in 006); noted, not changed |
| 15 | "exactly once" appears nowhere | Grep now covers root `README.md` (F14) |
