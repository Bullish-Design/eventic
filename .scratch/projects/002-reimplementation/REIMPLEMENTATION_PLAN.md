# Eventic — Reimplementation Plan

From today's `main` (`9a6c2e2`, 28 tests green) to the target in
`TARGET_ARCHITECTURE.md`. One commit per step; each step lists an **exit gate** and
**rollback**. The first three steps are **additive and keep the full suite green at
every commit** (success criterion §7); the behaviour-flipping steps come after,
each behind a deprecation window so 0.1.5 users have a boring upgrade path.

Convention: work on `reimagine/first-principles`. Run
`.venv/bin/python -m pytest src/tests -q` at every exit gate.

---

## Phase A — Non-breaking correctness & scaffolding (Steps 1–3, suite stays green)

### Step 1 — Kill the silent lost-update and the v0 asymmetry (R-C1, R-C2)

The single highest-value change, and it's nearly local.

1. **Deterministic `version_id` for v0.** In `record.py`, stop defaulting
   `version_id` to `uuid4`; set it in `model_post_init` to
   `uuid5(NAMESPACE_URL, f"eventic:{id}:0")` right before the v0 append (mirroring
   the mutation path). Now *all* versions have stable keys.
2. **Loud conflict in the store.** In `store.py.append`, detect the
   different-writer collision and raise instead of swallowing it. Replace bare
   `ON CONFLICT DO NOTHING` with: attempt the insert; on `(id, version)` conflict,
   `SELECT version_id` for that `(id, version)` — if it **equals** the row we tried
   to write, it's a replay → no-op; if it **differs**, raise
   `StaleVersionError(id, version)`. (Keep it dialect-portable: catch
   `IntegrityError` from a plain `insert`, then disambiguate with the select.)
3. **Rewrite the one test that blesses the bug.**
   `test_concurrent_mutations_do_not_duplicate_versions` currently asserts
   `fresh.title in ("from A","from B")` — passing *because* B was silently dropped.
   Rewrite it to assert that B's second write raises `StaleVersionError` and that
   history is exactly `[v0, v1]` with A as the winner. Add
   `test_replay_is_idempotent_but_conflict_is_loud`.

**Exit gate:** full suite green (with the rewritten concurrency test); probe_02
re-run now shows a raised `StaleVersionError` instead of silent loss.
**Rollback:** revert the commit; the store change is self-contained.

### Step 2 — Add the explicit API additively (R-E1, R-P1), old behaviour intact

Introduce the target surface *alongside* the current one; change no defaults yet.

1. `connect(url)` in a new `eventic/connect.py` — thin wrapper over the existing
   `init_eventic(create_engine(_normalize_db_url(url)))`. Idempotent. (Does **not**
   touch DBOS.)
2. On `Record`, add `save()`, `update(**fields)`, `commit()`, and an `edit()`
   context manager that batches multiple field sets into **one** version (dumps
   once, validates once, appends once — directly addressing R-P1). These are new
   methods; the existing `__setattr__` copy-on-write still works exactly as before.
3. Tests: `test_save_is_explicit`, `test_update_returns_new_version`,
   `test_edit_batches_one_version`, `test_construct_is_pure_when_using_save` (this
   last one uses a subclass created with the *future* default — see Step 4 — so for
   now it's marked `xfail(strict=False)` or deferred to Step 4).

**Exit gate:** full suite green; new explicit-API tests pass; construction still
auto-persists (unchanged) so all 28 originals pass untouched.
**Rollback:** delete `connect.py` and the new methods; nothing else references them.

### Step 3 — Carve out `eventic.dbos` adapter, keep a compat shim (R-P3, R-M2)

Move the DBOS coupling behind an optional package **without breaking imports**.

1. Create `eventic/dbos/__init__.py` exporting `create_app`, `durable` (== the old
   `@evented`'s registration, now explicit `DBOS.step()`), and `queue()`.
2. Move `queues/dispatcher.py` logic and `runtime.Eventic.create_app` into the
   adapter. Keep `eventic/runtime.py` as a **thin deprecation shim** that re-exports
   `Eventic` and warns once — so `from eventic import Eventic` and every existing
   test keeps working.
3. Add the `[dbos]` extra in `pyproject.toml`; keep `dbos` in the default deps for
   now (removing it is Step 6) so nothing breaks yet.
4. Make the core (`record.py`, `store.py`, `connect.py`, `models.py`, `events.py`)
   import-clean of `dbos` — verify with
   `python -c "import sys; import eventic; assert 'dbos' not in sys.modules"` **only
   after** Step 6 removes the shim's eager import; for now assert the *new* modules
   don't import dbos.

**Exit gate:** full suite green; `import eventic.dbos` works; core modules named
above contain no `import dbos` (grep gate).
**Rollback:** the shim makes this reversible — restore `runtime.py`, delete
`eventic/dbos/`.

> **After Step 3 the success criterion is met:** three commits, suite green at each,
> silent data loss fixed, explicit API available, DBOS isolated. Steps 4–7 complete
> the repositioning and are each independently shippable.

---

## Phase B — Behaviour flip (Steps 4–5, each behind a deprecation window)

### Step 4 — Make construction pure by default; add `hair_trigger` opt-in (R-E1, R-C3)

1. `Record` gains `__init_subclass__(hair_trigger=False)`. Default: construction is
   **pure** (no append, no create event); persistence requires `.save()`.
   `hair_trigger=True` restores today's auto-persist + auto-version-on-`=` for
   scripting.
2. Emit a `DeprecationWarning` from the auto-persist path for one minor release so
   existing implicit-write code keeps working but is flagged.
3. Migrate the library's own callers: `examples/demo.py`, `examples/webhook.py`
   (`main.py`), and the tests move to explicit `.save()`/`.update()`. Un-defer the
   Step-2 `test_construct_is_pure` test.
4. `on.create`/`on.update` → `on_commit` (post-commit). Keep `on` as a deprecated
   alias mapping to post-commit create semantics.

**Exit gate:** full suite green under the new default; a dedicated
`test_construction_does_no_io` passes; `pytest -W error::DeprecationWarning` is green
only for the migrated internal callers.
**Rollback:** flip the default back to `hair_trigger=True` globally (one line) — the
mechanism stays, only the default moves.

### Step 5 — Fold `properties` into `data.meta`; drop the column (R-P2 dup, L6)

1. Add optional `meta: dict[str, JSON] = {}` field to `Record`; delete
   `PropertiesBase` and the `_owner` back-pointer. `where()` queries `data` (incl.
   `data.meta`) instead of the separate column.
2. **Data migration** (`migrations/versions/xxxx_fold_properties_into_data.py`):
   ```sql
   -- copy the old bag into data.meta for every row, then drop the column
   UPDATE records SET data = jsonb_set(data, '{meta}', properties) ;      -- PG
   ALTER TABLE records DROP COLUMN properties ;                           -- PG
   ```
   SQLite path: rebuild the table (SQLite can't drop columns pre-3.35 portably) via
   the standard create-new/copy/drop/rename dance; provide it in the migration's
   `if dialect == 'sqlite'` branch. Ship the PG **GIN index** on `data` here too.
3. Backfill for v0 rows written before Step 1 keep their *random* `version_id`;
   that's fine (the `UNIQUE(id, version)` still protects them) — document that only
   rows written from 0.2+ have deterministic v0 keys.

**Exit gate:** suite green; `where()` tests pass against `data.meta`; the migration
round-trips (`upgrade` then a read) on a scratch SQLite **and** a scratch Postgres.
**Rollback:** the migration's `downgrade` re-adds the `properties` column and copies
`data.meta` back; additive and reversible.

---

## Phase C — Delete the machinery & re-document (Steps 6–7)

### Step 6 — Delete metaclass, frozen hack, singleton, `_session` fallback (R-M1)

1. Remove `RecordMeta` and `frozen=True`; `Record` is now a plain `BaseModel` whose
   `save/update/commit` do the persistence. Delete the COW `__setattr__` (the
   `hair_trigger` path reimplements a minimal version via `__setattr__` only on
   flagged subclasses).
2. Delete the `Eventic(DBOS)` subclass and the process singleton; the `eventic.dbos`
   adapter owns its own DBOS handle. Remove the deprecation shim from Step 3.
3. Remove `dbos` from default dependencies — it now lives only under `[dbos]`.
   `_session()`'s ambient-transaction handling moves fully into the adapter (which
   inspects DBOS context properly instead of catching `AssertionError`).

**Exit gate:** `python -c "import sys, eventic; assert 'dbos' not in sys.modules"`
passes; core suite (no-DBOS) runs in **~1s not ~35s** (probe_03 predicts this);
adapter suite runs under the `[dbos]` extra.
**Rollback:** this is the point of no return for the shim; keep Steps 1–5 as the
fallback release if Step 6 destabilizes.

### Step 7 — README rewrite, deprecation notes, final validation

1. Rewrite the README to the TARGET_ARCHITECTURE §7 positioning: versioned pydantic
   core, opt-in DBOS. Replace the "hair-trigger" and "concurrency contract" sections
   with the explicit-commit model and the **loud** `StaleVersionError` story.
   Document `hair_trigger` as the opt-in scripting mode.
2. A `MIGRATION.md` for 0.1.5→0.2: `Eventic.init`→`connect`, `s.x = y`→`.update`,
   `properties`→`meta`, `@evented`→`eventic.dbos`, and the DB migration order.
3. Final validation matrix:

| check | command |
|---|---|
| Core import is DBOS-free | `python -c "import sys, eventic; assert 'dbos' not in sys.modules"` |
| Core suite (SQLite) | `pytest src/tests/core -q` (fast, no DBOS) |
| Adapter suite | `pip install -e '.[dbos]' && pytest src/tests/dbos -q` |
| No hidden writes | `test_construction_does_no_io` green |
| Loud conflicts | `test_conflict_raises_stale_version` green |
| Migrations | `alembic upgrade head && alembic downgrade base` on scratch SQLite **and** PG |
| Warnings clean | `pytest -W error` green |

**Exit gate:** every row passes; `git log` shows one commit per Step 1–7.

---

## Migration story for existing `records` tables (0.1.5 → 0.2)

Ordered, boring, reversible:

1. **Step 1 ships first and alone** (deterministic v0 + loud conflicts) — no schema
   change, no data change; just corrects behaviour. Safe to deploy immediately.
2. **Step 5 migration** folds `properties`→`data.meta` and drops the column, with a
   working `downgrade`. Review the `UPDATE` on a staging copy first (as the existing
   `a1b2c3d4e5f6` dedupe migration already advises).
3. Existing duplicate-`(id,version)` rows, if any survived the pre-0.2 bug, are
   already handled by the shipped `a1b2c3d4e5f6` backfill; run it before adding new
   constraints.
4. No data loss: every step is additive or reversible; the only destructive
   operation (dropping `properties`) is preceded by copying it into `data.meta` in
   the same transaction.

## Rollback plan (overall)

- Steps 1–3 are independent, self-contained commits (`git revert` any one).
- Steps 4–5 are guarded by deprecation windows and reversible migrations.
- Step 6 is the irreversible cut; treat the Step-5 state as a shippable 0.2-beta so
  the project can pause there if the DBOS-optional split needs more soak time.
- If usage telemetry ever shows `@evented`/queues are heavily used *and* aggregates
  are single-writer (so R-C1 never fires), stop after Step 3: you'll have fixed the
  data-loss bug and isolated DBOS without paying the full-rewrite cost.
