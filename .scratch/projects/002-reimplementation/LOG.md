# Reimplementation — Working Log

## 2026-08-04 — Session 1 (branch `reimagine/first-principles`)

**Read (in the §1 order):** `README.md`; `001-code-review/REVIEW.md` (C1–C6, H1–H8,
M1–M12, L1–L12); `001-code-review/REFACTORING_GUIDE.md` incl. **Appendix B**
(execution deviations — the honest record of where the incremental plan fought
itself); every file under `src/eventic/` and `src/tests/`; `pyproject.toml`,
`dbos-config.yaml`, `alembic.ini`, both migrations.

**Baseline:** `.venv/bin/python -m pytest src/tests -q` → **28 passed in 34.9s**
(≈1.25s/test — the slowness is itself a data point, see probe_03). Deps: dbos
2.29.0, sqlalchemy 2.0.51, pydantic 2.11.7, fastapi 0.115.13, alembic 1.19.0.

**Probes written & run (all against the repo `.venv`, SQLite):**

- `probe_01_durable_v0_and_amplification.py` — **[verified]**
  - Constructing `Story(...)` writes a `records` row *immediately* (1 INSERT).
  - v0's `version_id` is **random uuid4**; only mutation version_ids are the
    deterministic uuid5. The README's "deterministic for every mutation" claim
    has an unadvertised v0 exception.
  - A 4-touch edit session (construct + 2 field sets + 1 `props.add`) → **4 full-row
    INSERTs**, each preceded by a full-model re-validation.
  - The L4 no-op guard avoids the write but *still* constructs+validates a whole
    new object to discover the no-op.
- `probe_02_concurrency_silent_lost_update.py` — **[verified]**
  - Two readers of v0 edit **disjoint fields**; the second write derives the same
    `(id, version=1)`, is dropped by `ON CONFLICT DO NOTHING`, and **vanishes with
    no error**. The losing in-memory object still reports the value it "wrote."
    The README sells this mechanism as a correctness feature; in the standalone
    path it is a silent lost-update with no SERIALIZABLE retry to save it.
- `probe_03_dbos_lifecycle_cost.py` — **[verified]**
  - init 17ms + launch 130ms + **destroy/reset ~979ms** ≈ **1.1s per process/test**
    of pure DBOS lifecycle; the actual value delivered (versioned write + read) is
    **13.8ms**. DBOS creates **11 system tables** next to eventic's single
    `records` table. This is what makes the suite take 35s and what every plain
    script pays for.
- `probe_04_pickle_queue_args.py` — **[verified]**
  - DBOS 2.29 default serializer is **`py_pickle`** (base64+pickle). A whole
    `Record` serializes to 504 b64 chars as a queue arg. A crafted arg executes
    code on `deserialize()` — RCE if any trust boundary reaches an enqueue arg or
    the DBOS system DB.

**Decisions reached this session (delta from initial reading is recorded in
REIMAGINE_REVIEW.md §1):**

1. The *storage kernel* (append-only single table; `id` + `version`;
   deterministic mutation `version_id`; JSONB `data`) is sound — keep it.
2. Everything wrapped *around* it fights the goal: copy-on-write-on-`__setattr__`,
   durable-v0-at-construction, the `RecordMeta` metaclass + per-class queue +
   `@evented`, the `PropertiesBase._owner` back-pointer, the `Eventic(DBOS)`
   subclass/singleton, and — decisively — **DBOS as a mandatory substrate**.
3. Verdict: **thin rewrite**. Rebuild the public API around explicit
   `save()/update()/commit()`, fold properties into normal fields, and demote
   DBOS to an **optional adapter** (`eventic[dbos]`). Full argument, incl. the
   honest "just delete the library" counter, in REIMAGINE_REVIEW.md §3.

**Deliverables produced this session:** `REIMAGINE_REVIEW.md`,
`TARGET_ARCHITECTURE.md`, `REIMPLEMENTATION_PLAN.md`, `probes/` (01–04), this log.

**Open / next session:** prototype Step 1–2 of the plan (pure `Record`, `connect()`,
explicit `save/update`) on this branch and confirm the suite stays green at each
step; add a Postgres-marked concurrency test that asserts the *loud* IntegrityError
behaviour the rewrite introduces.

## 2026-08-04 — Session 2 (design dialogue → conceptual docs)

Explored, in conversation, three architectural questions and captured the durable
conclusions as two new design documents:

- Should this be built on **SQLModel**? Conclusion: only if the product pivots to
  typed-column tables — which trades away the cheap append-only versioning that
  defines eventic and raises (not lowers) the metaclass machinery the review told us
  to cut. Keep the JSONB kernel; SQLModel is a *persistence-seam* option, not the base.
- **DBOS as a mixin, events as the core?** Yes — this is the right decomposition:
  the version log *is* the event stream; delivery of events is a swappable backend
  (`sync` default, `durable` via DBOS). It resolves R-C4 (pre-commit events), R-S1
  (pickled Records), R-P3 (DBOS tax), and the same-name-class crash as side effects.
- **Incremental diff storage?** Doable as a *storage codec* invisible above
  `hydrate()`; composes with events + DBOS, but fights the SQLModel typed-column
  option. Recommend forward-delta + snapshot-every-K (preserves append-only);
  opt-in, off by default, only worth it for large aggregates.

**New deliverables:**
- `CONCEPT.md` — the irreducible idea (log-is-the-event-stream) and the seven
  invariants (I1–I7) + the canonical write/read pipeline every plugin extends.
- `PLUGINS.md` — a general, closed-set (five-seam) plugin framework where
  durable-delivery, diff-storage, typed-columns, multi-tenancy, encryption, etc. are
  all the same kind of thing (a provider at a seam). Includes the anti-gold-plating
  guardrails (§8) and the rule: *don't build the framework before the second real
  plugin exists.*

These two are conceptual/forward-looking; they don't change the Plan's first three
(already-green) steps. `TARGET_ARCHITECTURE.md` §1 (events) and the `eventic.dbos`
adapter should be re-expressed in plugin/seam terms on its next revision.

**Also produced:** `REWRITE_GUIDE.md` — a detailed step-by-step guide for a *complete*
rewrite of the library around CONCEPT + PLUGINS (distinct from the incremental
`REIMPLEMENTATION_PLAN.md`). 14 steps in 6 phases: invariant core (Phase 1, shippable
DBOS-free 0.2-alpha) → extract the five-seam plugin framework (Phase 2) → DBOS delivery
plugin (Phase 3) → diff-storage codec plugin (Phase 4, the second plugin that justifies
the framework) → public surface + data migration + docs (Phase 5) → delete old code +
validation (Phase 6). One commit per step, exit gates, rollback, and a finding→step map
closing every R-* finding. Honors the PLUGINS §8.5 guardrail: framework extracted only
once the second real plugin needs it; each phase ends shippable so scope can be trimmed.
Suggested build branch: `rewrite/plugin-core` off this one.

## 2026-08-04 — Session 3 (branch `rewrite/plugin-core`) — Step 0

**Read (in the §1 order):** CONCEPT.md, PLUGINS.md, REWRITE_GUIDE.md,
TARGET_ARCHITECTURE.md, REIMAGINE_REVIEW.md (skim), the four probes, the current
source (`core/record.py`, `core/properties.py`, `persistence/{models,store}.py`,
`queues/dispatcher.py`, `events.py`, `runtime.py`, `bootstrap.py`, `main.py`,
`examples/demo.py`), `pyproject.toml`, both migrations.

**Baseline reproduced:** `.venv/bin/python -m pytest src/tests -q` → **28 passed
in 34.8s** (matches LOG session 1). Re-ran all four probes — probe_01 (durable v0,
uuid4 v0 version_id, 4-INSERT amplification, no-op re-validation), probe_02 (B's
write silently lost, object claims success), probe_03 (≈1.1s DBOS lifecycle vs
≈14ms value; 11 DBOS tables), probe_04 (py_pickle default; crafted arg executes) —
all confirm the documented bugs against the current code.

**Branch:** `git checkout -b rewrite/plugin-core` off `reimagine/first-principles`.

**Built (Step 0):**
- `src/eventic/errors.py` — full error hierarchy (EventicError, NotConnected,
  StaleVersionError with (id, version) attrs, PluginConflictError, MissingCapability).
- Empty-with-docstring skeleton for the target module map: `connect.py`,
  `models.py`, `record.py`, `pipeline.py`, `plugins/{__init__,persistence,codec,
  identity,delivery,interceptor}.py`, `dbos/__init__.py`, plus
  `src/tests/{core,plugins,dbos}/__init__.py`.
- **Deviation D1** (appendix): the new event core is `eventbus.py` for now —
  `events.py` is occupied by the old 0.1 module the old suite still imports
  (`@on.create`); renamed to `events.py` at the Step 12 swap.

**Verified:** skeleton imports cleanly; old suite still **28 green**.
**Committed:** `Step 0: skeleton + errors`.

## 2026-08-04 — Session 3 (cont.) — Steps 1–3

**Step 1 — connect() + engine registry + models.** `connect(url, *,
create_tables=True)` with re-connect swap + dispose; `engine()` raises
`NotConnected` pre-connect; `_reset()` test hook; `models.py` `records` table
(no `properties` column; `(id, version)` unique; `ix_records_id_ver`; JSONB
variant). 4 tests, 0.10s. Old suite untouched.

**Step 2 — Record pure construction.** `Record` = plain pydantic (not frozen,
no metaclass), managed fields id/version/version_id/created_ts/meta,
`model_post_init` stamps deterministic uuid5 v0 identity only (I3/I4).
`test_construction_writes_nothing` proves construction is I/O-free even while
connected. 9 core tests total.

**Step 3 — persistence + codec + identity + explicit writes.**
`SingleTableJSONB.append` loud-conflict logic; `latest/at/stream/query` row
primitives; `FullSnapshot` encode/decode + `fetch` read-hint; `Uuid5Deterministic`;
`record.save/update/commit/get/history/where` through `pipeline.commit_version`
and `pipeline.read/history`. Deviations D2 (replay check must compare
`(version_id, data)` — see appendix), D3 (`_uuid5` moved to identity plugin to
break an import cycle), D4 (codec `fetch` read-hint). **probe_02's scenario now
raises `StaleVersionError`** (verified inline); 21 core tests in 0.70s; old
suite still 28 green.
**Committed:** Steps 1, 2, 3.

## 2026-08-04 — Session 3 (cont.) — Steps 4–5, Phase 1 tag

**Step 4 — edit() batch writes.** `_EditProxy` (draft-copy namespace, nested
mutations land on the copy) + exit-time field diff; one `update` per batch,
no-op on empty/identical edits; exception in the with-block writes nothing.
Pydantic's `model_dump` copies nested dicts (verified) so the original object
stays untouched (I3). 7 tests.

**Step 5 — events core (I7).** `eventbus.py` (working name, D1): `Event` +
`on_commit(*classes, kind="*", mode="sync")` + `_HANDLERS` registry (class-
object keyed, MRO + registration order). `SyncDelivery` runs matching sync
handlers strictly post-persist, isolated (logged, never propagated). Handlers
receive the `Event` (D5). Pipeline `commit_version` emits exactly once per
commit; `save`→create, `update`/`edit`/`commit`→update with the field delta.
9 tests incl. H6 timing, delta receipt, class-object keying, MRO, isolation.

**Deviation D6** (I6 static until Step 9 — old `__init__.py` still imports
DBOS; live assertion lands with the Step-9 rewrite). Also added the Step-13
DBOS-free invariant as a static source check.

**Phase 1 exit gate:** 49 core tests in **1.18s** (old suite ≈35s → R-P3
closed); old suite still 28 green. Core upholds I1–I7. Tagged `0.2.0-alpha`.
**Committed:** Steps 4, 5; tag `0.2.0-alpha`.

## 2026-08-04 — Session 3 (cont.) — Step 6 (Phase 2)

**Step 6 — the five-seam plugin framework.** `Seam` enum + `EXCLUSIVE` set +
`Plugin` base (provides/requires/priority/mode) + the delivery mode registry
(global mode→backend; second backend for one mode conflicts; `sync` always
exists) + `use()` global defaults + `assemble()`: conflict check on exclusive
seams at class definition, capability resolution over the *effective* provider
pool (D7), `__eventic_plugins__` introspection, per-class seam instances,
interceptor priority ordering. Defaults converted to `Plugin` subclasses
(`SingleTableJSONB`, `FullSnapshot`, `Uuid5Deterministic`, `SyncDelivery`);
`TypedTable` shipped as the documented non-implemented stub (guardrail proof);
`Interceptor` ABC with `Veto`. `Record.__init_subclass__` calls `assemble`
(plugins = plugin bases in MRO). Pipeline dispatches through
`cls._persistence/_codec/_identity/_interceptors` + the delivery registry
(before_commit veto, after_commit isolated, after_hydrate on reads).
Deviations D7, D8. 12 assembly tests; all 49 Phase-1 tests green *through the
seam dispatch*; old suite still 28 green. **Committed:** Step 6.

## 2026-08-04 — Session 3 (cont.) — Step 7 (Phase 3, risky step)

**Probe first (probe_05, rewritten twice):** verified against the installed
dbos 2.29.0 source and live SQLite:
- `queue.enqueue` works from workflow/bare/handler contexts; **asserts
  `cur_ctx.is_workflow()` inside any @DBOS.transaction()**.
- Enqueued args cross the queue via the default serializer; str ids keep
  Records out of the system DB (R-S1).
- **D13 finding:** `init_workflow` writes the child row in the system DB's own
  transaction immediately — a failed parent workflow does NOT roll the enqueue
  back (nor completed txn-step app writes). The guide's "transactional
  outbox, atomic with the append" is not achievable as written on 2.29; the
  honest contract is at-least-once + idempotent handlers. probe_05 records it.
- **D12 finding:** SQLAlchemy `begin_nested()` + outer ROLLBACK does not roll
  back on SQLite (rows survived; raw sqlite3 rolls back fine) — so the ambient
  join uses check-then-insert instead of savepoints.

**Built:** `eventic/dbos/__init__.py` — `DurableEvents` (delivery seam,
`requires={"persistence:transactional"}`, enqueues `(handler_id, record_id)`
str args; loud `EventicError` if invoked inside a DBOS transaction; no-op when
no durable handlers match), `durable()` (explicit DBOS.step registration),
`queue()` (memoized handles — DBOS rejects duplicate Queue names),
`create_app()` (FastAPI + DBOS + eventic engine, no Eventic(DBOS) subclass);
ambient-session hook registering `DBOS.sql_session` into the persistence
plugin (appends join the transaction; a failed txn fn rolls the row back —
verified). `eventbus.on_commit` extended with `queue=` param, handler ids
(`module:qualname`), loud mode/queue validation at registration. pyproject:
dbos/fastapi/uvicorn moved to `[dbos]` extra; `pg` extra carries psycopg (D11).
`append` returns inserted-bool; pipeline delivers events only for real inserts
(I7 replay fix).

**8 dbos tests** (gated on the extra, skipif-import): handler runs after
commit via queue; id-not-record + system-DB bytes check (R-S1); replay fires
exactly one delivery; at-least-once across workflow abort (D13); transaction-
wrapped durable save raises loudly with nothing persisted; ambient join rolls
back a failed txn fn; explicit durable pattern end-to-end. Core (49) + plugins
(12) + old suite (28) all green. **Committed:** Step 7.

## 2026-08-04 — Session 3 (cont.) — Step 8 (Phase 4)

**Step 8 — DiffStorage codec (the second real plugin).** `DiffStorage(Plugin,
seam=CODEC, requires={"persistence:json"})` — full snapshot every K versions
(v0 and every K-th; K tunable per subclass via ClassVar, D15), forward
top-level field deltas otherwise; `decode` replays from the nearest snapshot;
`fetch` returns the snapshot→target window (exact-version KeyError preserved);
`head_state` reconstructs the true head for `where()` (D14 — pipeline-driven,
old persistence `query` deprecated). FullSnapshot gains the same `head_state`
hook. Size win: 50KB body edited 5× stores < 200KB (snapshot + tiny deltas).
Guardrail: `DiffStorage + TypedTable` → `MissingCapability` at definition.
7 tests incl. byte-for-byte equality with FullSnapshot at every version and
the K=2 snapshot/delta pattern. **Cross-suite fix (D16):** the plugins suite
now snapshot/restores the delivery registry (clearing it nuked the durable
backend the dbos suite registered at collection). All 75 new tests green
together; old suite 28 green. **Committed:** Step 8.

## 2026-08-04 — Session 3 (cont.) — Step 9 (Phase 5)

**Step 9 — public surface + examples.** New `__init__.py` exporting `Record,
connect, on_commit, use, DiffStorage, Plugin, Seam, StaleVersionError + the
error hierarchy`; D17: no auto-import of the dbos adapter (I6 + Step-13 live
check now pass in a fresh interpreter). `hair_trigger=True` escape hatch
(scripts only; violates I2; construction stays pure — verified). Rewrote
`examples/demo.py` (core-only SQLite demo, runs end-to-end) and
`examples/webhook.py` (was main.py; create_app + durable reindex + id-only
enqueue, M6 strict DTO). New webhook e2e test in the dbos suite (post →
persisted v0 → durable reindex SUCCESS; D19: wait inside the TestClient
window). D18: old test files removed (the swap makes their imports
impossible); old library modules stay until Step 12. 83 tests green; demo
runs. **Committed:** Step 9.

## 2026-08-04 — Session 3 (cont.) — Step 10 (Phase 5)

**Step 10 — data migration.** `fold_properties_into_data` revision
(down_revision = a1b2c3d4e5f6, so the C6 dedupe+unique backfill runs FIRST):
PG `jsonb_set` fold + `DROP COLUMN`; SQLite `json_set(data,'$.meta',
json(properties))` + Alembic batch table-rebuild (D20: the `json()` wrapper
parses the TEXT — without it the object becomes a JSON string; D21: raw-SQL
test inserts must use `.hex` for the Uuid storage format). Downgrade re-adds
`properties` from `data.meta` on both dialects. No second "fresh initial"
(D20 — one linear chain). Tests: old-library row hydrates under the new
library with `meta` folded; upgrade/downgrade round-trips on SQLite; the
fold's downgrade rebuilds properties from meta; a `postgres`-marked PG
round-trip (skipped here — CI). 3 passed + 1 skipped. **Committed:** Step 10.
