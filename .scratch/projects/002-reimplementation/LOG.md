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
