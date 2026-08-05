# Eventic — Target Architecture

Companion to `REIMAGINE_REVIEW.md` (verdict: **thin rewrite** — keep the storage
kernel, rebuild the public API around explicit commits, demote DBOS to an optional
adapter). This document is the destination; `REIMPLEMENTATION_PLAN.md` is the route.

Design values, in priority order: **(1) no hidden writes**, **(2) core has no DBOS
in its import graph**, **(3) fewer abstractions than today**, **(4) the storage
kernel and its data survive unchanged**.

---

## 1. Public API surface

### `eventic` (core — imports only pydantic + sqlalchemy)

```python
from eventic import Record, connect, on_commit

connect(url: str) -> None            # create+wire one engine; idempotent; replaces
                                     # Eventic.init/init_eventic for the no-DBOS path
```

`Record` (pydantic `BaseModel`, **not frozen**, **no metaclass**):

| member | behaviour |
|---|---|
| `Todo(**fields)` | **pure** — validates, assigns `id`; **no I/O** |
| `.save() -> Self` | INSERT v0 (idempotent on `(id,0)`); returns self; error if already saved |
| `.update(**fields) -> Self` | validate a **new** version, INSERT v_{n+1}, return the **new** object; original untouched |
| `.edit()` | context manager: batch several field sets into **one** version |
| `.commit() -> Self` | low-level: persist current in-memory state as the next version |
| `Todo.get(id, version=None)` | exact version (default latest); loud `KeyError` if absent |
| `Todo.history(id)` | iterator of versions oldest→newest |
| `Todo.where(**eq)` | latest records whose `data` matches (documented JSONB convenience) |
| `.meta` (optional) | one `dict[str, JSON]` field for free-form metadata (replaces the properties bag) |

`on_commit(*RecordClasses)` — register a **post-commit** callback (fires after the
row is durably written; keyed by class object; failures isolated+logged). Replaces
`on.create`/`on.update` and deletes the pre-commit timing footgun (R-C4).

### `eventic.dbos` (optional — only importable with `pip install eventic[dbos]`)

```python
from eventic.dbos import create_app, durable, queue

create_app(name, *, db_url, **fastapi) -> FastAPI   # FastAPI + DBOS, opt-in
@durable                                            # == DBOS.step(); registers fn
queue(name, *, concurrency=None) -> Queue           # explicit queue handle
```

No `Eventic(DBOS)` subclass, no process singleton in the core. The adapter holds its
own DBOS handle; `connect()` reuses DBOS's engine when the adapter is active (one
engine, one driver — the H5/R-P3 concern) and its own engine otherwise.

### Optional escape hatch: `hair_trigger`

```python
class ScratchNote(Record, hair_trigger=True):   # opt-in: restores s.x = y auto-write
    ...
```
Preserves the old "magic" for scripting **behind an explicit flag**; off by default,
so safety (no hidden writes, no R-C1) is the default and the delight is available to
those who knowingly choose it. Implemented as a subclass `__init_subclass__` hook,
not a metaclass.

---

## 2. The three examples (from review §1.3)

**Hello world (no DBOS, 5 lines):**
```python
from eventic import Record, connect
connect("sqlite:///app.db")
class Todo(Record):
    text: str; done: bool = False
t = Todo(text="learn eventic").save()
```

**Medium app (webhook + async, opt-in DBOS):**
```python
from eventic import Record, connect
from eventic.dbos import create_app, durable, queue
app = create_app("notes-svc", db_url=DB_URL)
class Note(Record):
    title: str | None = None; body: str | None = None
@durable
def reindex(note_id): search.index(Note.get(note_id))   # id in, re-hydrate
@app.post("/webhook")
async def hook(p: NoteIn):
    note = Note(title=p.title, body=p.body).save()
    queue("notes").enqueue(reindex, note.id)             # id-only arg (no pickled Record)
    return {"id": str(note.id)}
```

**Power user (history / migrations / multi-class):**
```python
cur   = Note.get(nid)                 # latest
v3    = Note.get(nid, version=3)      # exact; KeyError if missing
hist  = list(Note.history(nid))       # full lineage
pub   = Note.where(status="published")
with cur.edit() as e:                 # ONE new version for several edits
    e.title = "Final"; e.meta["status"] = "published"
```

---

## 3. Module map (before → after)

| today | fate | target |
|---|---|---|
| `core/record.py` (metaclass, frozen, COW `__setattr__`) | **rewrite** | `record.py` — plain pydantic, explicit `save/update/edit/commit`, `__init_subclass__` for `hair_trigger` |
| `core/properties.py` (`_owner` back-pointer) | **delete** | folded into optional `meta: dict` field |
| `persistence/models.py` | **keep** (drop the now-unused `properties` column after backfill) | `models.py` |
| `persistence/store.py` (`_session()` fallback) | **simplify** | `store.py` — one engine; ambient-DBOS session only via the adapter; **loud** `IntegrityError` on real conflicts (kills R-C1) |
| `queues/dispatcher.py` (`@evented`, `_queue_method`) | **delete from core** | `eventic/dbos/queue.py` — explicit `queue()`/`durable()` |
| `events.py` (sync, pre-commit) | **rewrite** | `events.py` — post-commit callbacks |
| `runtime.py` (`Eventic(DBOS)` singleton) | **delete** | `connect()` (core) + `eventic/dbos/app.py` (`create_app`) |
| `bootstrap.py` (`init_eventic`, subclass mutation) | **merge** into `connect()` | — |
| `main.py` webhook | **keep** (retarget to `eventic.dbos`) | `examples/webhook.py` |
| `examples/demo.py` | **keep** (simplify to new API) | `examples/demo.py` |

Net: **~9 modules → ~7**, and the two "aha-heavy" ones (metaclass, back-pointer) are
gone. Core import graph: pydantic + sqlalchemy only.

---

## 4. Data model (storage DDL — essentially unchanged)

```sql
CREATE TABLE records (
    version_id  UUID PRIMARY KEY,               -- deterministic uuid5(id, version) for ALL versions incl. v0 (fixes R-C2)
    id          UUID NOT NULL,
    version     INTEGER NOT NULL,
    class_type  VARCHAR NOT NULL,
    created_ts  TIMESTAMPTZ NOT NULL,
    data        JSONB NOT NULL,                 -- the full validated model (meta lives inside data.meta)
    CONSTRAINT uq_records_id_version UNIQUE (id, version)
);
CREATE INDEX ix_records_id ON records (id);
CREATE INDEX ix_records_id_ver ON records (id, version);          -- history/at_version
-- Postgres only, for where(): the GIN index the current repo omits
CREATE INDEX ix_records_data_gin ON records USING gin (data jsonb_path_ops);  -- PG
```

Two changes vs today: **(a)** `version_id` is `uuid5(NAMESPACE_URL,
"eventic:{id}:{version}")` for **every** version including v0 (removes R-C2's
asymmetry and makes v0 replay genuinely idempotent); **(b)** the separate
`properties` JSONB column is **dropped** after the backfill migration copies it into
`data.meta` (removes the L6 duplication). Everything else — same table, same PK, same
unique constraint — so existing rows are forward-compatible.

**Concurrency semantics (R-C1 fix), documented and loud:**
- Same `(id, version)` from a **crash-replay** → `ON CONFLICT DO NOTHING` no-op
  (idempotent — kept).
- Same `(id, version)` from **two different writers** → the store now **raises
  `StaleVersionError`** (wrapping `IntegrityError`) instead of silently dropping.
  Callers get an explicit optimistic-lock failure to retry/merge; under the DBOS
  adapter's SERIALIZABLE transactions DBOS retries automatically. The difference
  from today is one line — detect "row already exists with a *different*
  `version_id`" and raise — but it converts silent data loss into a normal,
  handleable error.

---

## 5. What gets deleted / renamed / added

**Deleted:** `RecordMeta` metaclass; `frozen=True` + COW `__setattr__` mutation
path; durable-v0-at-construction; `PropertiesBase` + `_owner`; per-class
`cls.queue`; `@evented` blanket magic; `Eventic(DBOS)` subclass + process singleton;
`_session()`'s `except AssertionError` fallback (moves into the adapter, done
properly via DBOS context inspection).

**Renamed / relocated:** `Eventic.init` → `connect` (core) / `create_app`
(adapter); `init_eventic` merged into `connect`; `on.create`/`on.update` →
`on_commit`; `src/eventic/main.py` → `examples/webhook.py`.

**Added:** explicit `save/update/edit/commit`; `StaleVersionError`;
`Record.get(id, version=…)` exact-version semantics with loud errors; optional
`meta` field; optional `hair_trigger=True`; the missing Postgres GIN index;
`eventic.dbos` adapter package; `eventic[dbos]` extra.

---

## 6. How each REIMAGINE finding is addressed

| finding | resolution |
|---|---|
| R-C1 silent lost update | store raises `StaleVersionError` on different-writer `(id,version)` collision (§4) |
| R-C2 v0 non-idempotent | deterministic `version_id` for **all** versions incl. v0 (§4) |
| R-C3 no identity map | explicit `update()` returns the new object; `hair_trigger` off by default removes stale-alias surprise; document "one aggregate, one editor per version" |
| R-C4 pre-commit events | `on_commit` fires post-commit only |
| R-C5 `_session` fallback | context handling moves into `eventic.dbos`; core uses one engine, no assertion catching |
| R-S1 pickle RCE | core ships no queue; adapter constrains enqueue args to ids/JSON and documents the pickle boundary |
| R-E1 hidden writes | explicit `save/update/edit`; construction is pure |
| R-P1 write amplification | `edit()` batches many fields into one version; no-op path no longer builds a throwaway object |
| R-P2 where() scan | documented as a convenience + ship the PG GIN index; primary path is by id |
| R-P3 DBOS tax | core has no DBOS; `connect()`+SQLite unit test is ~10ms, not ~1.1s |
| R-M1/M2 globals & metaclass | one module-level engine registry; no metaclass, no singleton, no back-pointer |
| R-M3 tests bless the bug | new suite asserts behaviour (lost update raises; construction does no I/O) |
| R-X1/X2 redundancy | shrink to the defensible pydantic-native core; compose DBOS instead of absorbing it |

---

## 7. Naming & positioning

The current README positions eventic as *"Pydantic on a hair-trigger + DBOS."* Both
halves are now wrong for the target: the trigger is opt-in, and DBOS is optional.
Keep the **name** `eventic` (it has a PyPI presence and the 001 history), but change
the **tagline** to:

> **eventic — versioned, persistent Pydantic. Plain Pydantic v2 models become
> immutable, version-tracked aggregates on Postgres or SQLite. Bring your own async
> (DBOS adapter included).**

This tells the truth about the 80% use case and stops enshrining the double-execution
and hidden-write behaviours as selling points (the 001 review's L12 lesson, one level
up: don't market the magic that generates the bugs).
