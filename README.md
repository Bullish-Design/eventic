# Eventic

> **A versioned document store with transactional change notification.**

A plain Pydantic model becomes a versioned document whose revision history is
the log, and whose commits are a transactional change feed. The log stores
*state per revision* — not named domain intents — and every commit writes the
log row, the head row, and every delivery intent in one database transaction.

- **Pure declarations.** `Stream`, `Subscription`, and `App` are frozen values.
  No decorators, no import-time registration, no ambient global state.
- **Explicit, store-bound writes.** Operations live on a `Collection` obtained
  from `App.bind(store)`. There is no `Record`, no `save()`, and no way to
  write without naming a database.
- **Compare-and-swap.** Every write carries an `expected_revision`; a stale,
  fabricated, or concurrent write raises `RevisionConflict`, loudly.
- **Append-only.** The log is never modified or deleted; heads are a derived
  projection rebuildable byte-exactly from the log (`eventic heads rebuild`,
  `eventic verify`).
- **Two backends.** SQLite (development, tests, single-process deployments) and
  PostgreSQL (production), proven by one identical conformance suite.
- **Durable at-least-once delivery.** Outbox subscriptions are transactional;
  the worker claims, delivers outside any transaction, and settles. Handlers
  must be idempotent. Inline subscriptions run after `COMMIT`, best-effort.

```python
from pydantic import BaseModel
from eventic import App, Stream, Subscription, Outbox
from eventic.sql import Postgres


class Todo(BaseModel):
    text: str
    done: bool = False


todos = Stream(Todo, name="todos", schema_version=1)

app = App(
    id="todo-service",
    streams=[todos],
    subscriptions=[
        Subscription(
            id="todo.reindex.v1",
            stream=todos,
            handler=reindex,
            delivery=Outbox(queue="search"),
        ),
    ],
)

ev = app.bind(Postgres(DATABASE_URL))

t = ev[todos].create(Todo(text="learn eventic"))  # Revision[Todo], revision 0
t = ev[todos].change(t, done=True)  # CAS on t.revision → revision 1

ev[todos].get(t.id)  # latest, from the head
ev[todos].get(t.id, revision=0)  # exact, from the log
ev[todos].history(t.id)  # Page[Revision[Todo]]
ev[todos].where(done=True)  # Page[Revision[Todo]]

with ev.batch() as b:  # one transaction, one commit
    b[todos].change(t, done=True)
    b[audits].create(Audit(action="todo.completed"))
```

Full static typing: `ev[todos]` returns `Collection[Todo]` derived from
`Stream[T]`, with no casts and no registry lookup.

## Installation

```console
pip install eventic               # SQLite + the pure core
pip install eventic[postgres]     # PostgreSQL driver
pip install eventic[migrate]      # alembic for eventic schema upgrade
```

## Quick start (SQLite)

```python
import sqlite3  # noqa
from pydantic import BaseModel
from eventic import App, Stream
from eventic.sql import SQLite


class Todo(BaseModel):
    text: str
    done: bool = False


todos = Stream(Todo, name="todos")
ev = App(id="demo", streams=[todos]).bind(SQLite("demo.db"))

t = ev[todos].create(Todo(text="learn eventic"))
t = ev[todos].change(t, done=True)
assert ev[todos].get(t.id).digest == t.digest
assert [r.revision for r in ev[todos].history(t.id).items] == [0, 1]
```

## Operations

```console
eventic --app myapp:app --url "$DATABASE_URL" schema upgrade
eventic --app myapp:app --url "$DATABASE_URL" schema check     # exits 3 on drift
eventic --app myapp:app --url "$DATABASE_URL" heads rebuild
eventic --app myapp:app --url "$DATABASE_URL" verify
eventic --app myapp:app --url "$DATABASE_URL" worker --queue search
eventic --app myapp:app --url "$DATABASE_URL" intents list --status dead
eventic --app myapp:app --url "$DATABASE_URL" intents redrive --subscription todo.reindex.v1
eventic --app myapp:app --url "$DATABASE_URL" inspect
```

`--app` names a `module:attr` that evaluates to an `App`. Every durable
declaration — stream name, subscription id — is a stable string you chose, so
refactoring a Python function never strands an outbox row.

## Delivery semantics

- **Inline** subscriptions run in the writing process after `COMMIT` returns,
  in declaration order. They are best-effort; a failure is raised as
  `InlineDispatchError` (or logged with `App(on_inline_error="log")`).
- **Outbox** subscriptions are delivered **at-least-once**: the intent is
  written in the same transaction as the commit; the worker claims it with a
  lease, delivers outside any transaction, and settles. If the process dies
  after a side effect but before the ack, the intent is delivered again.
  **Handlers must be idempotent.**
- Retries use exponential backoff with a cap; exhausted intents are
  dead-lettered and can be redriven.

## Schema evolution

Bump `schema_version` when the model changes and declare an upcaster chain:

```python
from eventic import App, Stream
from eventic.evolution import make_upcaster

todos = Stream(
    TodoV2,
    name="todos",
    schema_version=2,
    upcasters={1: make_upcaster(1, 2, lambda tree: {**tree, "priority": "normal"})},
)
```

Every row records its `schema_version`; reads upcast on the way out. A missing
upcaster is a *declaration* error, never a read-time surprise. The fingerprint
ledger (`eventic schema check`) catches a model change without a version bump.

## Documentation

| Guide | What it covers |
|---|---|
| [`docs/INVARIANTS.md`](docs/INVARIANTS.md) | The ten invariants, each made true by a mechanism, not a test |
| [`docs/DELIVERY.md`](docs/DELIVERY.md) | The delivery state machine in precise words |
| [`docs/EVOLUTION.md`](docs/EVOLUTION.md) | Schema evolution and the fingerprint ledger |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | Every operator action and its exit code |
| [`docs/STORE_AUTHORS.md`](docs/STORE_AUTHORS.md) | Writing a `Store`: the conformance suite is the spec |

## License

MIT
