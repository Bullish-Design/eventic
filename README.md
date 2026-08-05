# Eventic

[![PyPI version](https://img.shields.io/pypi/v/eventic?color=brightgreen)](https://pypi.org/project/eventic/) [![Python versions](https://img.shields.io/pypi/pyversions/eventic.svg)](https://pypi.org/project/eventic/) [![CI](https://img.shields.io/github/actions/workflow/status/Bullish-Design/eventic/ci.yml)](https://github.com/Bullish-Design/eventic/actions) ![License: MIT](https://img.shields.io/github/license/Bullish-Design/eventic.svg)

> **eventic — versioned Pydantic aggregates whose history is an event stream.**

A plain Pydantic v2 model becomes an immutable, version-tracked aggregate on
Postgres or SQLite. Pure-Python core (pydantic + SQLAlchemy only); durable
async, diff storage, and typed columns are opt-in plugins.

---

## The idea

A write is an **append**: mutating an aggregate never overwrites — it appends
a new immutable version, and the table of versions *is* the event stream.
Construction is **pure** (no I/O); writes are **explicit** (`save` / `update` /
`edit` / `commit`); concurrency conflicts are **loud**
(`StaleVersionError`), never silent; events fire **after** the row is durable,
exactly once per commit.

```python
from eventic import Record, connect

connect("sqlite:///app.db")

class Todo(Record):
    text: str
    done: bool = False

t = Todo(text="learn eventic").save()      # pure construct, then explicit save
t = t.update(done=True)                     # new version; the original is untouched
with t.edit() as e:                         # batch several edits into ONE version
    e.text = "learn eventic well"
    e.meta["priority"] = "high"

Todo.get(t.id)                              # latest version (exact version optional)
Todo.history(t.id)                          # the full version log, oldest → newest
Todo.where(**{"meta.priority": "high"})     # latest records whose head matches
```

## The invariants

| # | Invariant |
|---|---|
| I1 | **Append-only** — a committed version is immutable; writes only add rows. |
| I2 | **No hidden writes** — only `save`/`update`/`commit`/`edit` persist. |
| I3 | **Pure construction** — `Todo(...)` is in-memory only, no I/O. |
| I4 | **Deterministic identity** — `version_id = uuid5(NS, "eventic:{id}:{version}")` for every version, including v0. |
| I5 | **Loud conflicts** — two writers at one `(id, version)` raise `StaleVersionError`; only a byte-identical replay is a silent no-op. |
| I6 | **Core is DBOS-free** — `import eventic` never imports `dbos`/`fastapi`. |
| I7 | **One post-commit event** — events fire after durability, exactly once per commit. |

## The pipeline

```
construct ─► validate ─► before_commit ─► encode ─► persist ─► after_commit ─► emit ─► deliver
   (pure)    (pydantic)   (interceptors)  (codec)   (append; I1/I4/I5)        (one event, I7)
```

Reads follow `select → decode → after_hydrate → object` — nothing above the
codec seam knows how a version was stored.

## Events

```python
from eventic import on_commit

@on_commit(Todo, kind="create")            # fires after the row is durable
def log_new(event):
    print("created", event.record.id)

@on_commit(Todo, kind="update")
def log_delta(event):
    print("changed", event.delta)           # field-level changes
```

Handlers are keyed by the class object, run in MRO/registration order, and a
failing sync handler is logged and isolated — never propagated. Modes:

- `mode="sync"` (default) — in-process, immediately after commit.
- `mode="durable"` — via the optional `eventic[dbos]` extra: the handler runs
  later on a DBOS queue, receives the **id** (never a pickled Record), and
  must be **idempotent** (the async contract):

```python
from eventic.dbos import create_app, durable, queue

@durable
def reindex(note_id):                        # id in, re-hydrate, index
    Note.get(uuid.UUID(note_id)) ...

app = create_app("notes-svc", db_url=DB_URL)  # FastAPI + DBOS, opt-in
# inside a handler/workflow:
queue("notes").enqueue(reindex, str(note.id))
```

## Concurrency

The `(id, version)` pair is unique, and `version_id` is deterministic. Two
different writers at the same `(id, version)` raise `StaleVersionError` — an
optimistic-lock failure you retry or merge. Only a byte-identical replay (same
`version_id` **and** same bytes) is silently idempotent, which is what makes
crash-recovery replay safe.

## Plugins

Everything beyond the invariant core is a plugin occupying one of **five
seams** (`persistence`, `codec`, `identity`, `delivery`, `interceptor`).
Defaults are themselves the null plugins; conflicts and unmet requirements
fail **at class definition**, never at import or first call.

```python
class Doc(Record, DiffStorage):              # codec seam: forward deltas +
    body: str                                #   snapshot every K versions
class Order(Record, DurableEvents):          # delivery seam: durable outbox
    total: int
```

Ships: `SingleTableJSONB` (persistence), `FullSnapshot` + `DiffStorage`
(codec), `Uuid5Deterministic` (identity), `SyncDelivery` + `DurableEvents`
(delivery). `TypedTable` (typed columns) is a documented stub — a
demonstration of reach, not implemented.

## Installing

```bash
pip install eventic                # core: pydantic + SQLAlchemy + alembic
pip install "eventic[dbos]"        # + durable delivery (DBOS, FastAPI)
pip install "eventic[pg]"          # + the Postgres driver
```

Migrating from 0.1.x? See [MIGRATION.md](MIGRATION.md).
