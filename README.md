# Eventic

[![PyPI version](https://img.shields.io/pypi/v/eventic?color=brightgreen)](https://pypi.org/project/eventic/) [![Python versions](https://img.shields.io/pypi/pyversions/eventic.svg)](https://pypi.org/project/eventic/) [![CI](https://img.shields.io/github/actions/workflow/status/Bullish-Design/eventic/ci.yml)](https://github.com/Bullish-Design/eventic/actions) ![License: MIT](https://img.shields.io/github/license/Bullish-Design/eventic.svg)

> **eventic — versioned Pydantic aggregates whose history is an event stream.**

A plain Pydantic v2 model becomes an immutable, version-tracked aggregate on
Postgres or SQLite. Pure-Python core (pydantic + SQLAlchemy only); delta
storage and durable delivery are opt-in. Records are **frozen values** — every
write returns the new version; nothing mutates in place.

## The idea

A write is an **append**: mutating an aggregate never overwrites — it appends
a new immutable version, and the table of versions *is* the event stream.
Construction is **pure** (no I/O); writes are **explicit** (`save` / `update` /
`draft().commit()`); concurrency conflicts are **loud**
(`StaleVersionError`), never silent; events fire **after** the row is durable,
exactly once per commit.

```python
from eventic import Record, connect, on_commit

connect("sqlite:///app.db")

class Todo(Record):
    text: str = ""
    done: bool = False

t = Todo(text="learn eventic").save()      # pure construct, then explicit save
t = t.update(done=True)                     # new version; the original is untouched
d = t.draft()                               # batch several changes into ONE version
d.text = "learn eventic well"
d.meta["priority"] = "high"
t = d.commit()                              # commit RETURNS the new version

Todo.get(t.id)                              # latest version (exact version optional)
Todo.history(t.id)                          # the full version log, oldest → newest
Todo.where(**{"meta.priority": "high"})     # latest records whose head matches
```

## The invariants

| # | Invariant |
|---|---|
| I1 | **Append-only** — a committed version is immutable; the log only grows. |
| I2 | **No hidden writes** — only `save`/`update`/`draft().commit()` persist. |
| I3 | **Pure construction** — `Todo(...)` is in-memory only, no I/O. |
| I4 | **Deterministic identity** — `version_id = uuid5(NS, "eventic:{id}:{version}")` for every version, including v0. |
| I5 | **Loud conflicts** — two writers at one `(id, version)` raise `StaleVersionError`; only a byte-identical replay is a silent no-op. |
| I6 | **Core is DBOS-free** — `import eventic` never imports `dbos`/`fastapi`. |
| I7 | **One post-commit event** — events fire after durability, exactly once per commit. |
| I8 | **No process state outside a `Store`** — the store is explicit and context-bound. |

## The pipeline

```
construct ─► validate ─► before_commit ─► encode ─► append ─┐
   (pure)    (pydantic)   (interceptors,  (codec)  (log,    │  ONE TRANSACTION
                           may veto)                I1/I4/I5)│
                                          project head ──────┤
                                          stage outbox ──────┤
                                                             │
                                          ══ COMMIT ═════════╡  ◄── durability line
                                                             │
                             after_commit ◄──────────────────┤  (interceptors)
                             emit ─► deliver ◄───────────────┘  (I7: one event)
```

**The transaction emits, not the pipeline.** The pipeline stages events on the
unit of work; the unit of work flushes them only after `COMMIT` — so a version
that gets rolled back (a DBOS workflow abort, a caller's transaction) never
fires anything (I7). The schema is a triad:

```
eventic_log      append-only, immutable     — the truth
eventic_head     one row per aggregate      — derived; a cache with the same txn boundary
eventic_outbox   pending durable deliveries — drained, then reaped
```

`head` and `outbox` are derived and rebuildable (`eventic rebuild-heads`).

## The object model

```python
class Todo(Record, stream="todo", codec=Delta(k=20), interceptors=(Audit(),)):
    text: str = ""
```

Seams are selected by **class keyword**, never by inheritance — framework
types never enter your pydantic MRO, so subclassing works the way you expect.
`stream` defaults to the class name and **collides loudly** (one stream, one
class). `Delta` requires a JSON-shaped store — a type, checked at class
definition. `version_id` is a module function (`eventic.version_id`), not a
seam.

Records are **frozen** and `extra="forbid"` (a typo'd field is a loud
`ValidationError`, not a silently persisted row). Managed fields
(`id`/`version`/`version_id`) and commit metadata (`created_ts`) live in
columns and are merged back at hydration — so `created_ts` reflects when the
version was committed, and crash-recovery replays compare stable bytes (I5).

## Events

```python
@on_commit(Todo, kind="create")            # fires after the row is durable
def log_new(event):
    print("created", event.record.id)

@on_commit(Todo, kind="update")
def log_delta(event):
    print("changed", event.delta)          # field-level changes
```

Handlers are keyed by the class object, run in MRO/registration order, and a
failing sync handler is logged and isolated — never propagated. Delivery is a
property of the **subscription**:

- `via="inline"` (default) — in-process, immediately after commit.
- `via="outbox"` — durable: the row is staged inside the commit transaction
  and drained later, by `OutboxDispatcher` (in-process) or the DBOS driver.
  Durable handlers receive the **full Event** — the same object sync handlers
  get — and must be **idempotent**.

```python
@on_commit(Todo, via="outbox", queue="reindex")
def reindex(event): ...                    # runs later, as a DBOS step
```

## The Store

```python
from eventic import Store

with Store("sqlite:///app.db", create_tables=True):
    ...                                    # scoped: binds/unbinds the active store

connect("sqlite:///app.db")                # dev sugar: Store + DDL + activate
```

There is no module-global engine. `Store` defaults `create_tables=False`
(Alembic is the source of truth in production); `connect()` defaults `True`
for the README one-liner. `active_store()` raises `NotConnected` before a
store is bound — v2's no-op `use()` era is over.

## Concurrency

The `(id, version)` pair is unique, and `version_id` is deterministic. Two
different writers at the same `(id, version)` raise `StaleVersionError` — an
optimistic-lock failure you retry or merge. Only a byte-identical replay (same
`version_id` **and** same bytes) is silently idempotent, which is what makes
crash-recovery replay safe. The suite races 8 threads at one `(id, version)`:
**1 winner, 7 loud losers**, every run.

## DBOS (optional)

DBOS is a *driver*, not the mechanism: `DbosStore` joins the enclosing DBOS
workflow's transaction, and `DbosDispatcher` drains the outbox onto DBOS
queues — each handler runs as a DBOS step, so DBOS owns retries and recovery.

```python
from eventic.contrib.dbos import DbosStore, DbosDispatcher

store = DbosStore(DB_URL, create_tables=True).activate()
# inside a DBOS workflow or request handler:
DbosDispatcher(store).drain()              # enqueue one DBOS step per outbox row
```

## Installing

```bash
pip install eventic                # core: pydantic + SQLAlchemy + alembic
pip install "eventic[dbos]"        # + durable delivery (DBOS, FastAPI)
pip install "eventic[pg]"          # + the Postgres driver
```

Migrating from 0.2? See [MIGRATION.md](MIGRATION.md). The design contract is
[`docs/CONCEPT.md`](docs/CONCEPT.md).
