# Eventic

[![PyPI version](https://img.shields.io/pypi/v/eventic?color=brightgreen)](https://pypi.org/project/eventic/) [![Python versions](https://img.shields.io/pypi/pyversions/eventic.svg)](https://pypi.org/project/eventic/) [![CI](https://img.shields.io/github/actions/workflow/status/Bullish-Design/eventic/ci.yml)](https://github.com/Bullish-Design/eventic/actions) ![License: MIT](https://img.shields.io/github/license/Bullish-Design/eventic.svg)

> **Pydantic, on a hair-trigger.**

Eventic turns plain **Pydantic v2** models into immutable, version-tracked aggregates that persist to Postgres or SQLite and ride on **DBOS** durable queues & workflows.

---

## ✨ Features at a glance

| What                     | Why it matters                                                                                     |
| ------------------------ | -------------------------------------------------------------------------------------------------- |
| **Copy-on-write records** | Every attribute assignment creates a *new* immutable version row (version 0 is persisted at construction) |
| **Single-table storage** | All versions live in one `records` table with JSON(B) columns and a unique `(id, version)` constraint |
| **Opt-in `@evented`**    | Explicitly-marked methods are scheduled on the per-class DBOS queue — no double execution, plain methods run inline |
| **FastAPI one-liner**    | `Eventic.create_app()` wires FastAPI + DBOS, auto-launching workers on startup                     |
| **Script-friendly**      | Mutations persist even in plain scripts (no DBOS context required)                                 |
| **Free-form properties** | JSONB property bag with `add / remove / list` helpers that persist automatically                   |

---

## 🚀 Quick start

1. **Install**

```bash
pip install eventic
```

> Postgres is the default database path, so there is no `[pg]` extra anymore; the extra remains as an empty alias for compatibility.

2. **Create `.env`** (or export in your shell):

```bash
export DBOS_DATABASE_URL="postgresql://user:pass@localhost/eventic_demo"
```

3. **Run the canned demo**

```bash
python -m eventic.examples.demo
```

The demo spins up a FastAPI app, creates a `Story` record, walks it through several versions, and prints the final JSON snapshot.

---

## 🏗️ A minimal script (no web server)

```python
import os
from eventic import Eventic, Record

class Todo(Record):
    text: str

# 1️⃣  One-time init (creates the records table & injects the store)
Eventic.init(name="todo-svc", database_url=os.getenv("DBOS_DATABASE_URL"))

# 2️⃣  Start DBOS worker threads (needed for queued/async work)
Eventic.launch()

# 3️⃣  Use your record like any Pydantic model
item = Todo(text="Learn Eventic")
print(item.version)      # 0  (persisted at construction)
item.text = "Ship v1"    # copy-on-write ➜ version 1 row inserted
print(item.version)      # 1

# 4️⃣  Read it back — even from a fresh process
fresh = Todo.hydrate(item.id)
print(fresh.text)        # "Ship v1"
```

**How persistence works:** inside a DBOS transaction, writes go through the ambient transaction session (fast path). Outside any DBOS context, `RecordStore` falls back to a short-lived session on your configured engine and commits on clean exit — so the minimal-script flow above just works. Reads inside a transaction see the transaction's own uncommitted writes; reads outside it see committed rows.

---

## 🖥️ Full FastAPI example (taken from `examples/demo.py`)

```python
import os, uuid
from pprint import pprint
from eventic import Record, Eventic

# ── 1. FastAPI + Eventic bootstrap ──────────────────────────────
app = Eventic.create_app("eventic-demo", db_url=os.getenv("DBOS_DATABASE_URL"))

# ── 2. Domain model ────────────────────────────────────────────
class Story(Record):
    title: str | None = None
    body: str | None = None

# Auto-generated per-class queue for heavy jobs
story_q = Story.queue

# ── 3. DBOS steps ───────────────────────────────────────────────
@Eventic.transaction()
def create_story() -> uuid.UUID:
    return Story().id  # version 0 row (auto-persisted)

@Eventic.transaction()
def draft(sid: uuid.UUID, text: str):
    Story.hydrate(sid).body = text

@Eventic.step()
def snapshot(sid: uuid.UUID):
    pprint(Story.hydrate(sid).model_dump())

# ── 4. Durable workflow exposed at GET / ────────────────────────
@app.get("/")
@Eventic.workflow()
def demo_flow():
    sid = create_story()
    story_q.enqueue(draft, sid, "Once upon a time …")
    story_q.enqueue(snapshot, sid)
    return {"id": str(sid)}

# ── 5. Script entry-point ───────────────────────────────────────
if __name__ == "__main__":
    Eventic.launch()            # run DBOS workers in this process
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

---

## 🎯 Opt-in background methods: `@evented`

The `RecordMeta` metaclass attaches `_queue_name = "queue_<snake_case>"` and a single per-class `Queue` — it does **not** touch your methods. To schedule a method on the class queue, mark it explicitly:

```python
class Story(Record):
    title: str | None = None

    @evented
    def regenerate_preview(self):
        # runs later, on the class queue, over a serialized snapshot of self
        ...

story.regenerate_preview()   # enqueues; does NOT run synchronously
```

Semantics:

- `@evented` methods are **scheduled, not run inline** — there is no double execution.
- The queued run executes as a DBOS workflow against a serialized *snapshot* of `self`. For aggregate mutations, prefer passing `self.id` and re-hydrating inside the step so the queued run observes fresh state.
- Everything else — staticmethods, classmethods, properties, pydantic internals — is left completely untouched.

---

## ⚙️ How it works (deeper dive)

### Copy-on-write field mutation

Every `Record` is **frozen**; assigning to a public field constructs a *new* model with `version += 1`, validates the whole model, writes it to the store, and then reflects the **validated** state onto the in-memory object so local state always matches what was persisted. Assigning the same value is a no-op (no version is written). `version`, `version_id`, and `id` are aggregate-managed and cannot be assigned directly.

### Storage layout

```
records
┌────────────┬────────────┬────────┬──────────────┬─────────────────────────┬──────────────┬─────────┐
│ version_id │ id         │ version│ class_type   │ created_ts              │ properties   │ data    │
└────────────┴────────────┴────────┴──────────────┴─────────────────────────┴──────────────┴─────────┘
```

- `version_id` is the immutable primary key. For every mutation it is **deterministic**: `uuid5(NAMESPACE_URL, "eventic:{id}:{version}")`.
- `id` is the stable aggregate identifier you pass around.
- `version` + `id` are unique together (`uq_records_id_version`) — duplicate rows are impossible.
- `properties` holds the JSONB property bag (non-null); `data` holds the full validated model for hydration.

### Concurrency contract

The deterministic `version_id` + the `UNIQUE (id, version)` constraint + `ON CONFLICT DO NOTHING` make crash-recovery replays idempotent: a re-run of a DBOS transaction inserts the *same* row (a no-op) instead of a duplicate. For two genuinely different concurrent writers to the same aggregate:

- **Inside DBOS transactions** (default `SERIALIZABLE` isolation on Postgres): the loser aborts with a serialization error and **DBOS retries the whole transaction**, re-hydrating and bumping to the next free version.
- **In standalone scripts**: the collision is safely ignored by `ON CONFLICT DO NOTHING` (the deterministic version_id makes both writers produce the same row) — so the history never corrupts. Serialize per aggregate (e.g. the per-class `concurrency=1` queue) when you need last-writer-wins ordering.

### Querying

- `Story.hydrate(sid)` — latest committed row for that aggregate.
- `Story.hydrate(sid, at_version=3)` — newest row with `version <= 3` (scoped to `Story`'s `class_type`).
- `Story.where(status="published")` — hydrated records whose latest `properties` JSONB matches all given key/value pairs (class-scoped). Filter values are normalized (`uuid.UUID` → string, `datetime` → ISO).

### Event handlers

```python
from eventic import on

@on.create(Story)
def log_new_story(story): ...

@on.update(Story)
def log_updated_story(story): ...
```

- Handlers are keyed by the **class object** and run in registration order (base classes included).
- A failing handler is isolated: it is logged and the emitting construction/mutation proceeds.
- Timing: `create` fires *after* the version-0 row is persisted (handlers can hydrate); `update` fires after the append but **before** the transaction commits — treat the store as eventually-consistent within the emitting transaction.

---

## 🛠️ Configuration & deployment tips

| Concern                 | Script                                  | FastAPI                                          |
| ----------------------- | --------------------------------------- | ------------------------------------------------ |
| Connect Eventic         | `Eventic.init(name, database_url)`      | `Eventic.create_app(name, db_url)`               |
| Start DBOS workers      | `Eventic.launch()` (once)               | automatic via ASGI startup                       |
| Production workers      | `dbos` CLI / `uvicorn eventic.main:app` |                                                  |
| Database migrations     | `alembic upgrade head` (see `migrations/`) | `dbos-config.yaml` runs it via `database.migrate` |

Class names become queue names via snake_case (`Story` → `queue_story`). Keep class names unique across a process — DBOS's queue registry is keyed by queue name, so two same-named `Record` subclasses cannot coexist in one process.

### Lifecycle

`Eventic.init()`/`create_app()` may only be called **once per process** — a second call raises. Call `Eventic.reset()` to tear the singleton down (tests, multi-app setups) before re-initializing.

---

## 🤝 Contributing

Bug reports and pull requests are welcome! Please make sure `pytest` passes:

```bash
pip install -e '.[test]'
pytest src/tests -q        # SQLite — no Postgres required
```

The suite runs entirely on SQLite (DBOS 2.x uses the same sqlite file for its system database); Postgres-only assertions (JSONB containment, serialization retries) run under a `postgres` marker in CI.

## 📜 License

Eventic is released under the **MIT License**.
