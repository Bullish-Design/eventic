# Migrating to Eventic 0.3

0.3 is a **structural refactor** (see `.scratch/projects/003-structural-refactor`
for the reviewed evidence — 23 verified findings — and the design contract in
`docs/CONCEPT.md`). The public API, the on-disk schema, and the module layout
all change. No backwards compatibility: this is `0.3.0`.

The database migration is one Alembic revision (`0300_triad`) that rebuilds
from the old `records` table to the log/head/outbox triad.

## 1. Code changes

| 0.2 | 0.3 |
|---|---|
| `class Doc(Record, DiffStorage)` | `class Doc(Record, codec=Delta(k=20))` |
| `class Doc(Record, DurableEvents)` | `@on_commit(Doc, via="outbox", queue="q")` |
| `class Doc(Record, InterceptorPlugin)` | `class Doc(Record, interceptors=(Audit(),))` |
| `use(...)` / `Plugin` / `Seam` | **deleted** — seams are keywords and Protocols |
| `with d.edit() as e:` (batched) | `d = d.draft(); ...; d = d.commit()` (returns the new version) |
| `d.update(**kw)` | unchanged (returns the new version) |
| `connect(url)` then implicit global engine | `connect(url) -> Store` (explicit, context-bound); or `with Store(url):` |
| `set_ambient_session_provider(...)` | subclass `Store` and override `_begin()` (e.g. `DbosStore`) |
| `eventic.dbos.create_app(name, db_url=...)` | **deleted** — build the FastAPI app yourself |
| `@on_commit(cls, mode="durable", queue="q")` | `@on_commit(cls, via="outbox", queue="q")` |
| durable handler receives a bare **id** | durable handler receives the full **Event** (same as sync) |
| `hair_trigger=True` | **deleted** — records are frozen; `draft()` is the supported path |
| `Record(..., extra="allow")` | frozen + `extra="forbid"` — a typo'd field is a loud `ValidationError` |
| plugin mixin base classes | class keywords (`stream=`/`rows=`/`codec=`/`interceptors=`) |

Identity is now `eventic.version_id(id, version)` (a function, not a seam).
`StreamCollision` replaces v2's silent same-`__name__` log sharing — same-named
classes must declare distinct `stream=` names.

## 2. The durable contract (new)

- Subscriptions registered `@on_commit(Order, via="outbox", queue="orders")`
  are staged **inside the commit transaction** (atomic with the version row)
  and drained later. Handlers run with the **full `Event`** — the same object
  a sync handler receives — and must be **idempotent** (at-least-once).
- The outbox row is a *reference* (version_id + stream + id + version + kind +
  delta), never a pickled `Record`; the handler re-hydrates the record at that
  exact version on replay.
- Drain with `eventic drain --url ... [--queue Q]` (in-process), or the DBOS
  driver: `DbosDispatcher(store).drain()` from a workflow context enqueues one
  DBOS step per row — DBOS owns retries and recovery.

## 3. Database migration

```bash
alembic upgrade head
```

`0300_triad`:

1. Creates `eventic_log`, `eventic_head`, `eventic_outbox`.
2. Copies `records` → `eventic_log`:
   - `stream := class_type`, `committed_at := created_ts`,
     `kind := 'create' if version = 0 else 'update'`.
   - **Strips** the phantom plugin keys (`seam`/`provides`/`requires`/
     `priority`/`mode`) and the managed keys (`id`/`version`/`version_id`/
     `created_ts`) from `data` — without this, `extra="forbid"` rejects your
     own historical rows on read.
   - `snapshot := true` for old `FullSnapshot` rows; old `DiffStorage` rows
     are unwrapped (`snapshot` from `data->>'kind'`, `state`/`patch` into the
     new shapes; old deltas get `del: []` — they never recorded removals).
3. Builds `eventic_head` by replaying each stream (the same fold the codecs
   do; equivalent to `eventic rebuild-heads`).
4. Drops `records`.

`downgrade()` rebuilds `records` from `eventic_log` honestly: possible, but it
cannot restore the phantom fields, and does not pretend to.

> After upgrading, a stream that was stored with `DiffStorage` must be read by
> a class declaring `codec=Delta(...)` — the migrated rows are delta-shaped.
