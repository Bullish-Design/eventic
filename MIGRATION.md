# Migrating to Eventic 0.2

0.2 is a **rewrite** around explicit commits: construction is pure, writes are
loud, and DBOS is optional. Most code changes are mechanical renames; the data
migration is one Alembic upgrade.

## 1. Code changes

| 0.1.x | 0.2 |
|---|---|
| `Eventic.init(name=..., database_url=...)` | `connect(database_url)` |
| `Eventic.create_app(name, db_url=...)` | `eventic.dbos.create_app(name, db_url=...)` |
| `s = Story(...)` (auto-persists v0!) | `s = Story(...)` (pure) then `s = s.save()` |
| `s.title = "x"` (copy-on-write write) | `s = s.update(title="x")` |
| several `s.x = ...` in a row | `with s.edit() as e: e.x = ...` (one version) |
| `s.properties.add(status="x")` | `s = s.update(meta={"status": "x"})` (or `e.meta[...] = ...` in `edit`) |
| `Story.hydrate(id)` | `Story.get(id)` / `Story.get(id, version=n)` (exact; `KeyError` if absent) |
| `Story.hydrate(id, at_version=n)` ("≤ n") | `Story.get(id, version=n)` (exact) |
| `@on.create(Story)` / `@on.update(Story)` | `@on_commit(Story, kind="create"/"update")` |
| `@evented` methods (implicit queue) | `@eventic.dbos.durable` + explicit `queue(name).enqueue(fn, id)` |
| `Story.queue.enqueue(fn, self)` (pickled Record!) | `queue("name").enqueue(fn, str(id))` (id only) |
| `PropertiesBase` | plain `meta: dict` on `Record` |

`hair_trigger=True` (opt-in) restores the old implicit writes for scripts —
documented as "scripts only; violates I2", off by default:

```python
class ScratchNote(Record, hair_trigger=True):
    text: str
```

## 2. The durable contract (new)

- Handlers registered `@on_commit(Order, mode="durable", queue="orders")` run
  **later**, receive the **id as a str**, re-hydrate themselves, and must be
  **idempotent**. Delivery is at-least-once.
- `queue.enqueue` needs a *workflow* context (a bare `@DBOS.transaction()`
  cannot enqueue). Save durable-mode records from a workflow (a request
  handler in a `create_app` app, or a `@DBOS.workflow`), or use the explicit
  `queue(name).enqueue(fn, id)` pattern after the transaction.
- Enqueued args are plain ids/JSON — never pickled `Record`s (this closes the
  0.1 pickle-RCE surface).

## 3. Database migration

Run, in order, on a staging copy first:

```bash
alembic upgrade head
```

The chain is: initial → `a1b2c3d4e5f6` (C6 dedupe + `(id, version)` unique)
→ `fold_properties_into_data` (folds `properties` into `data.meta` and drops
the column). Fresh installs run the same chain to the new schema.

- **Data**: `properties` moves into `data.meta`. Old rows keep their (random)
  v0 `version_id` — I4's determinism only guarantees rows written by 0.2+; the
  unique constraint still protects them. The fold is reversible
  (`alembic downgrade -1` re-adds `properties` from `data.meta`).
- **No data loss**: the C6 backfill dedupes pre-0.2 duplicate `(id, version)`
  rows (keeping the newest) *before* the fold; review it on staging first.
- A table written by the 0.1 library hydrates under 0.2 (`data.meta` is
  visible as `record.meta`).
