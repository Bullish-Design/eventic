# Eventic 0.3 — Structural Refactor: Implementation Guide

**Goal:** rebuild eventic around `CONCEPT.md` (v3) — the transaction as the unit of
work, three protocol seams selected by keyword, records as frozen values, and a
log/head/outbox triad. Evidence for every change is in `REVIEW.md`, reproducible via
`probes/`.

**Target stack:** Python ≥3.13 · Pydantic 2.11 · SQLAlchemy 2.0.51 · DBOS optional.

**Ground rules**

- **No backwards compatibility.** This is `0.3.0`. The public API, the on-disk schema,
  and the module layout all change. One migration rebuilds existing data (Step 22).
- **One commit per step.** Every step has an **exit gate** and a **rollback**.
- **Always green.** Run `.venv/bin/python -m pytest src/tests -q` at every gate. The
  suite never goes red between commits; the refactor is incremental even though the
  destination is a near-rewrite of the plugin layer.
- **Finding-driven.** Step 0 turns all 23 findings into failing tests *first*. Each
  later step's exit gate names the findings it flips to passing. When the last one
  flips, the refactor is done — that is the definition, not a judgment call.

**Branch:** `git checkout -b refactor/transaction-core` off `main`.

---

## Target module layout

Dependencies flow strictly downward. No module imports a module below it, and nothing
imports at the bottom of a file to dodge a cycle — the `# import them last so the
submodules can safely import` dance at `plugins/__init__.py:167` is the clearest
signal the current layering is inverted.

```
src/eventic/
  errors.py              # the exception hierarchy (leaf)
  identity.py            # version_id(id, version) — I4, a function, not a seam
  event.py               # Event
  seams.py               # RowStore / JsonRowStore / Codec / Interceptor Protocols
  config.py              # EventicConfig, MRO resolution, the stream registry
  store/
    schema.py            # eventic_log / eventic_head / eventic_outbox
    unit_of_work.py      # UnitOfWork — the transaction boundary + staged events
    sql.py               # SqlStore: the default RowStore (stateless)
    __init__.py          # Store, active_store(), connect()
  codec/
    snapshot.py          # Snapshot (default)
    delta.py             # Delta (forward deltas + tombstones + snapshot every K)
  interceptors.py        # Interceptor base + Veto
  subscribe.py           # on_commit, Subscription, the handler registry
  pipeline.py            # commit / read orchestration
  record.py              # Record + Draft
  dispatch/
    inline.py            # in-process, post-commit
    outbox.py            # OutboxDispatcher.drain()
  contrib/
    dbos.py              # DbosStore + DbosDispatcher (~50 lines)
  cli.py                 # eventic rebuild-heads | drain
  examples/
  __init__.py            # the public surface
```

Test layout:

```
src/tests/
  regression/   # one test per REVIEW.md finding — the refactor's definition of done
  core/         # record, config, identity, errors
  store/        # unit of work, sql store, transactions, concurrency
  codec/        # snapshot, delta, tombstones, windows
  dispatch/     # inline, outbox drain
  contrib/      # dbos — opt-in, skipped without the extra
```

---

# Phase 0 — Safety net

## Step 0 · Turn every finding into a failing test

Before changing a line of library code, encode `REVIEW.md` as an executable
specification. This is the single highest-leverage step in the guide: it converts a
prose review into a definition of done that cannot be argued with.

1. Record the baseline: `86 passed, 1 skipped`.
2. Create `src/tests/regression/test_findings.py` with **one test per finding**,
   named `test_f01_phantom_fields_not_persisted`, etc. Each asserts the *correct*
   behavior and is marked `@pytest.mark.xfail(strict=True, reason="F1 — see REVIEW.md")`.
   Lift the assertions straight out of `probes/`.
3. `strict=True` matters: when a step accidentally fixes a finding, the xfail turns
   into an `XPASS` failure and you find out immediately rather than at the end.
4. Add `src/tests/regression/test_no_reset_hooks.py`, the mechanical proxy for I8 —
   currently xfail:

   ```python
   def test_no_process_globals():
       """I8: no mutable process state outside a Store."""
       offenders = [p for p in _package_modules() if _has_reset_hook(p)]
       assert offenders == []
   ```

**Exit gate:** `86 passed, 1 skipped, 24 xfailed`. No library code changed.
**Rollback:** delete `src/tests/regression/`.

> From here on, every step reports its gate as `N xfailed` shrinking toward zero.

---

# Phase 1 — The transaction owns the truth

The load-bearing phase. It alone fixes the broken invariant (F3), and everything after
it depends on the `UnitOfWork` existing.

## Step 1 · The schema triad

Create `store/schema.py`. New tables — the old `records` table stays untouched and
in use until Step 22.

```python
class LogRow(Base):                      # the truth: append-only, immutable (I1)
    __tablename__ = "eventic_log"
    version_id   = Column(Uuid, primary_key=True)          # uuid5(id, version) — I4
    stream       = Column(String, nullable=False)
    id           = Column(Uuid, nullable=False)
    version      = Column(Integer, nullable=False)
    kind         = Column(String, nullable=False)          # 'create' | 'update'
    snapshot     = Column(Boolean, nullable=False)         # codec-declared; enables §14
    committed_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    data         = Column(JSONB, nullable=False)           # USER STATE ONLY (CONCEPT §5.1)
    __table_args__ = (
        UniqueConstraint("id", "version", name="uq_eventic_log_id_version"),   # I5
        Index("ix_eventic_log_stream_id_version", "stream", "id", "version"),
        Index("ix_eventic_log_snapshot", "stream", "id", "version",
              postgresql_where=text("snapshot"), sqlite_where=text("snapshot")),
    )

class HeadRow(Base):                     # derived: one row per aggregate, rebuildable
    __tablename__ = "eventic_head"
    stream     = Column(String, primary_key=True)
    id         = Column(Uuid, primary_key=True)
    version    = Column(Integer, nullable=False)
    version_id = Column(Uuid, nullable=False)
    state      = Column(JSONB, nullable=False)             # fully decoded head state
    __table_args__ = (Index("ix_eventic_head_state", "state", postgresql_using="gin"),)

class OutboxRow(Base):                   # derived: pending durable deliveries
    __tablename__ = "eventic_outbox"
    seq          = Column(BigInteger, primary_key=True, autoincrement=True)
    version_id   = Column(Uuid, nullable=False)
    stream       = Column(String, nullable=False)
    record_id    = Column(Uuid, nullable=False)
    version      = Column(Integer, nullable=False)
    kind         = Column(String, nullable=False)
    delta        = Column(JSONB, nullable=True)
    handler_id   = Column(String, nullable=False)
    queue        = Column(String, nullable=False)
    attempts     = Column(Integer, nullable=False, default=0)
    available_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (
        UniqueConstraint("version_id", "handler_id", name="uq_eventic_outbox_once"),
        Index("ix_eventic_outbox_ready", "available_at"),
    )
```

Three notes, each deliberate:

- **One composite index replaces three overlapping ones** (F18), and `stream` — which
  every query filters on and which had no index at all — is now the leading column.
- **`snapshot` is a column, not a JSON key.** A delta codec's window query becomes a
  single indexed range scan instead of introspecting `data->>'kind'` (Step 14).
- **`UNIQUE(version_id, handler_id)`** makes outbox staging idempotent under replay,
  mirroring I5 at the delivery layer.

**Exit gate:** tables create on SQLite and Postgres; suite unchanged.
**Rollback:** delete the module.

## Step 2 · `UnitOfWork` — the durability line

Create `store/unit_of_work.py`. **This is the fix for F3 and the spine of the whole
design.** The pipeline will never emit again; it stages, and the transaction flushes.

```python
_CURRENT: ContextVar[UnitOfWork | None] = ContextVar("eventic_uow", default=None)

class UnitOfWork:
    def __init__(self, session: Session, *, owns_commit: bool):
        self.session, self._owns = session, owns_commit
        self._staged: list[Event] = []

    @classmethod
    def current(cls) -> UnitOfWork | None:
        return _CURRENT.get()

    def stage(self, event: Event) -> None:
        self._staged.append(event)

    def __enter__(self) -> UnitOfWork:
        self._token = _CURRENT.set(self)
        if not self._owns:
            # We do NOT control COMMIT. Bind to the owner's signal instead, so the
            # durability line holds identically on both paths.
            sa_event.listen(self.session, "after_commit", self._flush, once=True)
            sa_event.listen(self.session, "after_rollback", self._discard, once=True)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        _CURRENT.reset(self._token)
        if not self._owns:
            return False                  # the owner commits; our listener flushes
        if exc_type is not None:
            self.session.rollback()
            self._staged.clear()
            return False
        self.session.commit()             # ◄── THE DURABILITY LINE
        self._flush()
        return False

    def _flush(self, *_) -> None:
        staged, self._staged = self._staged, []
        for event in staged:
            dispatch_inline(event)        # isolated per handler; never propagates

    def _discard(self, *_) -> None:
        self._staged.clear()
```

Nesting: a nested `unit_of_work()` returns a proxy that stages into the parent and
never commits, so an inner write inside an outer transaction emits once, with the
outer.

**Tests (`store/test_unit_of_work.py`):** events flush only after commit · rollback
discards staged events · an exception inside the block rolls back and emits nothing ·
nested UoWs emit once, with the outermost · a foreign session's commit flushes via
the `after_commit` listener.

**Exit gate:** new tests green; suite unchanged (nothing calls it yet).
**Rollback:** delete the module.

## Step 3 · `Store` — the explicit, context-bound object

Create `store/__init__.py`; **delete `connect.py`** and its `_ENGINE` global.

```python
_ACTIVE: ContextVar[Store | None] = ContextVar("eventic_store", default=None)

class Store:
    def __init__(self, url: str, *, create_tables: bool = False):
        self.engine = create_engine(_normalize_pg(url), future=True, pool_pre_ping=True)
        if create_tables:
            Base.metadata.create_all(self.engine)

    def _begin(self) -> tuple[Session, bool]:
        """(session, do_we_own_the_commit). Overridden by integrations."""
        return Session(self.engine, future=True), True

    def unit_of_work(self) -> UnitOfWork:
        if (cur := UnitOfWork.current()) is not None:
            return _Nested(cur)                  # stage into the parent; never commit
        session, owns = self._begin()
        return UnitOfWork(session, owns_commit=owns)

    @contextmanager
    def session(self) -> Iterator[Session]:      # reads
        with Session(self.engine, future=True) as s:
            yield s

    def activate(self) -> Self:                  # bind; returns self for chaining
        self._token = _ACTIVE.set(self)
        return self

    def deactivate(self) -> None:
        _ACTIVE.reset(self._token)

    def __enter__(self) -> Self: return self.activate()
    def __exit__(self, *exc) -> bool: self.deactivate(); return False

def active_store() -> Store:
    if (s := _ACTIVE.get()) is None:
        raise NotConnected("no active Store — call eventic.connect(url) or use `with Store(url):`")
    return s

def connect(url: str, *, create_tables: bool = True) -> Store:
    """Dev sugar: build a Store, create tables, activate it, return it."""
    return Store(url, create_tables=create_tables).activate()
```

Three decisions worth stating:

- **`ContextVar`, not a module global.** Async-safe and thread-safe (a module global is
  neither), and scope exit unbinds — so **no `_reset` hook is needed**, which is how
  I8 gets satisfied rather than asserted.
- **`Store(...)` defaults `create_tables=False`; `connect(...)` defaults `True`.** Two
  entry points, two honest defaults: the production constructor does not silently DDL
  your database, and the dev one-liner still works in the README (F23).
- **`_begin()` is the integration point** that replaces the global
  `set_ambient_session_provider` hook. An integration subclasses `Store`; it does not
  mutate process state.

**Tests:** `active_store()` raises `NotConnected` before binding · `with Store(...)`
scopes correctly · two stores coexist in one process · concurrent threads see their
own binding.

**Exit gate:** `test_connect.py` rewritten; suite green.
**Rollback:** restore `connect.py`.

## Step 4 · Route writes through the UnitOfWork — **F3 dies**

Change `pipeline.commit_version` to stage instead of emit, and change the persistence
append to take a session from the UoW rather than opening its own. Keep the existing
plugin wiring intact for now — this step is about *when* things happen, not *how they
are declared*.

```python
with active_store().unit_of_work() as uow:
    inserted = cls._persistence.append(uow.session, row)
    if inserted:
        uow.stage(Event(kind=kind, record=new, delta=delta))
# emission happens here, after COMMIT, via the UoW
```

Delete `_ambient_session`, `set_ambient_session_provider`, and
`_reset_ambient_session_provider`. The `check-then-insert` shape and its D12 rationale
**stay** — that was a correct fix for a real SQLAlchemy savepoint problem, and
`probe_06` proves the concurrency behavior it protects.

**Exit gate:** `F3` flips to pass — a handler no longer fires for a version that was
rolled back. `probe_04`'s I7 section reports `>>> ok`. Concurrency test still shows
**1 winner, 7 loud losers**. `22 xfailed`.
**Rollback:** revert; the UoW is unused again but harmless.

---

# Phase 2 — Declaration replaces inheritance

## Step 5 · `seams.py` — three Protocols

```python
@runtime_checkable
class RowStore(Protocol):
    def append(self, s: Session, row: LogRow) -> bool: ...
    def read(self, s: Session, stream: str, id: UUID, window: Window, version: int | None) -> list[LogRow]: ...
    def head(self, s: Session, stream: str, id: UUID) -> HeadRow | None: ...
    def upsert_head(self, s: Session, head: HeadRow) -> None: ...
    def search(self, s: Session, stream: str, eq: Mapping[str, Any]) -> list[HeadRow]: ...
    def stage_outbox(self, s: Session, sub: Subscription, event: Event) -> None: ...

@runtime_checkable
class JsonRowStore(RowStore, Protocol):
    """Marker: `data` is an opaque JSON document. `Delta` requires this."""

class Codec(Protocol):
    requires: ClassVar[type] = RowStore
    def encode(self, prev: Record | None, new: Record) -> Encoded: ...
    def decode(self, rows: Sequence[LogRow]) -> Json: ...
    def window(self) -> Window: ...

class Interceptor(Protocol):
    def before_commit(self, record: Record) -> Record: ...   # THREADED (F11)
    def after_commit(self, event: Event) -> None: ...        # observer, isolated
    def after_hydrate(self, record: Record) -> Record: ...    # threaded
```

`Encoded = (data: Json, snapshot: bool)`; `Window = POINT | SINCE_SNAPSHOT`.

**RowStore implementations are stateless** — they receive a `Session` rather than
holding an engine. That is what lets a class declare its store at definition time
without coupling to a connection, which in turn is what lets the seam-compatibility
check run at class definition.

**Note the interceptor fix:** `before_commit` is now genuinely a transformer, symmetric
with `after_hydrate`. v2 documented enrichment and discarded the return value (F11).

## Step 6 · `config.py` — resolution and the stream registry

```python
@dataclass(frozen=True, slots=True)
class EventicConfig:
    stream: str
    rows: RowStore
    codec: Codec
    interceptors: tuple[Interceptor, ...]

_STREAMS: dict[str, type] = {}          # declaration, not state (CONCEPT §3.1)

def register_stream(name: str, cls: type) -> None:
    if (prior := _STREAMS.get(name)) not in (None, cls):
        raise StreamCollision(
            f"stream {name!r} is already claimed by {prior.__module__}.{prior.__qualname__}; "
            f"give {cls.__qualname__} an explicit stream=..."
        )
    _STREAMS[name] = cls
```

Loud collisions replace v2's silent `cls.__name__` sharing (F13).

## Step 7 · Keyword selection — **delete `plugins/`**

Rewrite `Record.__init_subclass__`:

```python
def __init_subclass__(cls, *, stream=None, rows=None, codec=None,
                      interceptors=None, **kw):
    super().__init_subclass__(**kw)
    inherited = getattr(cls, "__eventic__", DEFAULT_CONFIG)
    cfg = EventicConfig(
        stream       = stream or cls.__name__,        # NOT inherited: one stream per class
        rows         = rows         if rows         is not None else inherited.rows,
        codec        = codec        if codec        is not None else inherited.codec,
        interceptors = tuple(interceptors) if interceptors is not None else inherited.interceptors,
    )
    if not isinstance(cfg.rows, cfg.codec.requires):          # loud, AT CLASS DEFINITION
        raise SeamMismatch(
            f"{cls.__name__}: {type(cfg.codec).__name__} requires "
            f"{cfg.codec.requires.__name__}, but {type(cfg.rows).__name__} does not provide it"
        )
    register_stream(cfg.stream, cls)
    cls.__eventic__ = cfg
    cls.__subscriptions__ = []
```

Then **delete the entire `plugins/` package**: `Plugin`, `Seam`, `assemble`,
`register_delivery`, `_DELIVERY_MODES`, `_DELIVERY_INSTANCES`, `_GLOBAL_PLUGINS`,
`use()`, `provides`/`requires` tokens, `TypedTable`, `contribute_schema`,
`full_state_rows`, `Uuid5Deterministic`, `SingleTableJSONB.query()`.

Move `_uuid5` to `identity.py` as `version_id(id, version)` — a function, per I4
(F9). Move the codecs to `codec/`, the interceptor base to `interceptors.py`, and
`SingleTableJSONB` to `store/sql.py` as `SqlStore`.

`PluginConflictError` is **deleted, not renamed**: with keyword selection, two codecs
on one class is not an error to raise — it is a sentence you cannot write.
`MissingCapability` becomes `SeamMismatch`.

**Exit gate — the big one.** These flip to pass:

- **F1** — `Doc.model_fields` for a `codec=Delta()` class equals a plain Record's;
  `data` contains no `seam`/`provides`/`requires`/`priority`/`mode`.
- **F2** — `class SubDoc(Doc)` inherits the codec and does not crash on required fields.
- **F8** — `use()` no longer exists (its test asserts `ImportError`).
- **F9** — identity is a function; no dead seam.
- **F12** — the dead surface is gone.
- **F13** — duplicate stream names raise `StreamCollision`.

`16 xfailed`.
**Rollback:** this step is large; keep it a single revertible commit and do not mix
Step 8 into it.

---

# Phase 3 — Subscriptions and dispatch

## Step 8 · Subscriptions live on the class

Create `subscribe.py`; delete `events.py`'s `_HANDLERS` / `_HANDLER_IDS` globals.

```python
@dataclass(frozen=True, slots=True)
class Subscription:
    kind: str; fn: Callable; via: str; queue: str | None; handler_id: str
    def matches(self, kind: str) -> bool:
        return self.kind in ("*", kind)

_HANDLERS: dict[str, Callable] = {}      # declaration, not state (CONCEPT §3.1)

def on_commit(*classes, kind="*", via="inline", queue=None):
    if via not in ("inline", "outbox"):
        raise ConfigError(f"unknown delivery {via!r}")
    if via == "outbox" and not queue:
        raise ConfigError("outbox subscriptions must name a queue")

    def deco(fn):
        hid = f"{fn.__module__}:{fn.__qualname__}"
        if _HANDLERS.setdefault(hid, fn) is not fn:
            raise HandlerCollision(hid)          # LOUD, not first-wins (F22)
        sub = Subscription(kind, fn, via, queue, hid)
        for c in classes:
            c.__dict__["__subscriptions__"].append(sub)
        return fn
    return deco

def subscriptions_for(cls) -> list[Subscription]:
    return [s for c in cls.__mro__ for s in c.__dict__.get("__subscriptions__", ())]
```

MRO inheritance for free, no global registry, no cross-test leakage, **no reset hook**.

**Exit gate:** F22 flips (duplicate handler ids raise). `15 xfailed`.

## Step 9 · Inline dispatch, and the interceptor contract

`dispatch/inline.py` runs matching `via="inline"` subscriptions with per-handler
isolation (log and continue — unchanged and correct). In `pipeline.commit`, thread
`before_commit`'s return value:

```python
for itc in cfg.interceptors:
    new = itc.before_commit(new)          # F11
```

and hand `after_commit` the `Event` rather than the record, matching handlers.

**Exit gate:** F11 flips. `14 xfailed`.

## Step 10 · The real outbox — **F10 dies**

Outbox rows are written **inside the same transaction** as the log row, which is what
makes durable delivery atomic by construction rather than by capability token.

```python
with store.unit_of_work() as uow:
    if cfg.rows.append(uow.session, log_row):
        cfg.rows.upsert_head(uow.session, head_row)
        for sub in subscriptions_for(cls):
            if sub.via == "outbox" and sub.matches(kind):
                cfg.rows.stage_outbox(uow.session, sub, event)
        uow.stage(event)
```

`dispatch/outbox.py`:

```python
class OutboxDispatcher:
    def drain(self, store: Store, *, queue: str | None = None, limit: int = 100) -> int:
        """Claim ready rows, rebuild the Event, run the handler, delete on success."""
        # SELECT ... WHERE available_at <= now() [AND queue = ?]
        #   ORDER BY seq LIMIT :limit FOR UPDATE SKIP LOCKED   (plain SELECT on SQLite)
        # per row: rehydrate Record.get(id, version=version) -> Event -> fn(event)
        #   success -> DELETE;  failure -> attempts += 1, available_at = backoff
```

The durable handler receives a **full `Event`**, identical to a sync handler — v2's
bare-id signature existed only because the outbox was fake. `event.record` is the
record *at that version* (not the current head), which is the correct semantic for an
idempotent replay.

Delivery is now selected **per subscription**. There is no delivery seam, no
`register_delivery`, and no way for a backend to reach a class that never opted in.

**Exit gate:** F10 flips — a subscription on class A never fires for class B. Outbox
rows roll back with the version row on abort. `13 xfailed`.

## Step 11 · `contrib/dbos.py` — DBOS becomes a driver

Rewrite the whole `dbos/` package as ~50 lines:

```python
class DbosStore(Store):
    def _begin(self):
        try:
            return DBOS.sql_session, False      # join the workflow's transaction
        except AssertionError:
            return super()._begin()

class DbosDispatcher:
    """Drains the outbox onto DBOS queues; each handler runs as a DBOS step."""
```

Deleted along the way: `create_app` (an app factory is not a persistence library's
job), the `"durable delivery cannot enqueue inside a @DBOS.transaction()"` error — an
error that existed *only* because the architecture was wrong and is now
inexpressible — and the module-level `register_delivery(DurableEvents)` side effect.

Rewrite `examples/webhook.py` so the app is built by a function the user calls, not by
`app = build_app()` at import (F20).

**Exit gate:** `contrib/` tests green under the extra, skipped without it. I6 import
test still passes. F20 flips, and the last `_reset_*` hook disappears with `_reset_queues`, so the **I8 gate** flips too. `11 xfailed`.

---

# Phase 4 — The read path

## Step 12 · Maintain the head projection

`upsert_head` writes `(stream, id) → (version, version_id, state)` in the commit
transaction. `state` is the full decoded user state, so it is codec-independent.

Guard against out-of-order writes with `WHERE eventic_head.version < :version` on the
update branch. Under concurrency the log's unique `(id, version)` already serializes
writers, so only the winner reaches the upsert — but the guard makes replay and
backfill safe too.

**Tests:** head tracks the log for both codecs · head survives a rolled-back
transaction unchanged · head is byte-identical to a full replay of the log.

## Step 13 · `get`/`where` off the head — **F16 dies**

- `get(id)` (no version) → one indexed `eventic_head` row. Same cost for every codec.
- `where(**eq)` → one query with real pushdown:
  - **Postgres:** `state @> :json` against the GIN index; dotted keys become nested
    JSON (`{"meta": {"priority": "high"}}`).
  - **SQLite:** `json_extract(state, '$.meta.priority') = :v` AND-ed per key.

Delete the Python-side `_match` / `_get_path` scan and the `read()`-per-match second
pass.

> **Guardrail applied (CONCEPT §8.1).** `where()` supports equality, so build equality
> pushdown — **not** a general predicate AST. Build the AST when the second predicate
> kind arrives. Speculative generality is what produced the capability-token system
> this refactor is deleting.

**Exit gate:** F16 flips — `where()` on 10 aggregates is **2 SQL statements, not 12**.
`10 xfailed`.

## Step 14 · Bounded historical reads — **F17 dies**

The codec declares its window; the store answers with one range query.

```python
# Window.SINCE_SNAPSHOT
WHERE stream = :s AND id = :id
  AND version <= :v
  AND version >= (SELECT max(version) FROM eventic_log
                   WHERE stream = :s AND id = :id AND snapshot AND version <= :v)
```

Backed by the partial index from Step 1. A delta read touches at most `K` rows
regardless of how long the aggregate has lived.

**Exit gate:** F17 flips — `get()` on an 800-version delta aggregate reads ≤ `K` rows,
and the 18 ms/read becomes ~1 ms. `9 xfailed`.

## Step 15 · `history()` as a linear fold

Replace the `rows[:i+1]` re-decode with a single forward pass that carries the running
state, yielding one object per row. O(N) instead of O(N²) (F19).

**Exit gate:** F19 flips. `8 xfailed`.

## Step 16 · `cli.py`

```
eventic rebuild-heads --url URL [--stream S]    # head is derived; prove it
eventic drain --url URL [--queue Q]             # run the outbox dispatcher
```

`rebuild-heads` is the honesty check on CONCEPT §2.1: if the projection cannot be
rebuilt from the log, the log is not the truth. Add a test that mutates a head row,
rebuilds, and asserts recovery.

**Exit gate:** CLI tests green.

---

# Phase 5 — Value semantics

## Step 17 · Frozen, and `extra="forbid"`

```python
model_config = ConfigDict(frozen=True, extra="forbid")
```

Derive `version_id` in a `mode="before"` validator so it is always computed and never
client-set — which the v2 docstring already claimed but did not enforce:

```python
@model_validator(mode="before")
@classmethod
def _derive_identity(cls, data):
    if isinstance(data, dict):
        data.setdefault("id", uuid4())
        data.setdefault("version", 0)
        data["version_id"] = version_id(data["id"], data["version"])
    return data
```

This deletes every `object.__setattr__` workaround, including the `_hair_live`
attribute that v2 wrote into every instance's `__dict__`.

> **Decided trade-off, stated plainly:** freezing prevents *rebinding fields*. It does
> not deep-freeze container contents — `t.meta["k"] = v` still mutates in memory, as
> does any user `list` field. Deep immutability of arbitrary user data is not
> achievable, so do not half-fake it with a `MappingProxyType` on `meta` alone.
> Document the boundary and make `draft()` the supported path.

**Exit gate:** F14 flips — `Todo(txt="typo")` raises instead of persisting. Expect
fallout in tests that relied on `extra="allow"`; fix them, do not weaken the config.
`7 xfailed`.

## Step 18 · `draft()` replaces `edit()`; delete `hair_trigger` — **F6 dies**

```python
class Draft:
    """Mutable scratch copy. Nothing is written until commit()."""
    def commit(self) -> Record:
        changes = self._changed_fields()
        return self._base if not changes else self._base.update(**changes)
```

```python
d = t.draft()
d.text = "learn eventic well"
d.meta["priority"] = "high"
t = d.commit()                 # returns the NEW version — assignment is the point
```

**No context-manager form is provided, deliberately.** A `with` block cannot return a
value, which is precisely the mechanism of F6: v2's `edit()` computed a new version and
discarded it, stranding the caller on a stale handle — visible in the shipped demo's
own output. Making the result an assignment makes stranding unrepresentable.

Delete `hair_trigger` entirely, plus `_EditProxy`, `_hair_commit`, `_hair_live`, and
the `cls.__setattr__ = ...` monkeypatch. A library cannot ship a flag whose purpose is
disabling its own invariant I2. If the scripting ergonomics are genuinely wanted, they
belong in a separate `eventic.scripting` shim, clearly outside the invariant core.

Guard `save()`, which v2 left open:

```python
def save(self) -> Self:
    if self.version != 0:
        raise UsageError("save() persists v0; use update() for later versions")
```

**Exit gate:** F6 flips — the demo prints the correct version. `6 xfailed`.

## Step 19 · Column-merge hydration — **F5 dies without breaking I5**

`encode` dumps **user fields only**; managed fields and commit metadata live in
columns and are merged back at hydration:

```python
MANAGED = frozenset({"id", "version", "version_id", "created_ts"})

def user_state(rec) -> Json:
    return rec.model_dump(mode="json", exclude=MANAGED)

def hydrate(cls, state, row):
    obj = cls.model_validate(state | {
        "id": row.id, "version": row.version,
        "version_id": row.version_id, "created_ts": row.committed_at,
    })
    for itc in reversed(cls.__eventic__.interceptors):
        obj = itc.after_hydrate(obj)
    return obj
```

> **Why this shape and not the obvious one.** Stamping `created_ts` into `data` would
> fix F5 and *silently break I5*: a crash-recovery replay would produce different
> bytes, so the byte-identical-replay no-op would become a `StaleVersionError`.
> Splitting state from commit metadata satisfies both, shrinks `data`, and gives future
> commit metadata (`actor`, `causation_id`, `trace_id`) a home that does not touch the
> codec contract. Add a regression test asserting a replay is still a silent no-op
> *after* this change — that interaction is the easiest thing in the guide to get wrong.

**Exit gate:** F5 flips (`created_ts` populates) **and** the I5 replay test still
passes. `5 xfailed`.

---

# Phase 6 — Codec correctness and errors

## Step 20 · Delta tombstones — **F4 dies**

```python
# snapshot row: data = <full user state>,   snapshot=True
# delta row:    data = {"set": {...}, "del": [...]},  snapshot=False

def encode(self, prev, new) -> Encoded:
    after = user_state(new)
    if prev is None or new.version % self.k == 0:
        return Encoded(after, snapshot=True)
    before = user_state(prev)
    return Encoded({"set":  {k: v for k, v in after.items() if before.get(k, _MISS) != v},
                    "del":  sorted(before.keys() - after.keys())},
                   snapshot=False)

def _apply(state, patch):
    state.update(patch["set"])
    for k in patch["del"]:
        state.pop(k, None)
    return state
```

Both codecs now share one convention — *a snapshot row's `data` is the full user
state* — so `Snapshot.decode` is `rows[-1].data` and `Delta.decode` folds forward from
the snapshot. Add a property test: for a random sequence of field adds/changes/removes,
`get(v)` equals the in-memory object at every `v`.

**Exit gate:** F4 flips. `4 xfailed`.

## Step 21 · The error hierarchy

```python
class EventicError(Exception): ...
class NotConnected(EventicError): ...
class RecordNotFound(EventicError, KeyError): ...   # F15 — satisfies both contracts
class StaleVersionError(EventicError): ...          # I5
class StreamCollision(EventicError): ...
class HandlerCollision(EventicError): ...
class SeamMismatch(EventicError): ...
class ConfigError(EventicError): ...
class UsageError(EventicError): ...
class Veto(EventicError): ...                       # exported (F12)
```

`RecordNotFound` inherits both so `except KeyError` keeps working and `errors.py`'s own
"everything derives from `EventicError`" claim becomes true. Fix the unimported
`Callable` annotation (F21) — it dies with the module anyway. Confirm the
`query()` `NameError` (F7) is gone with `plugins/`.

**Exit gate:** F7, F15, F21 flip. `1 xfailed`.

---

# Phase 7 — Land it

## Step 22 · The migration

One Alembic revision, `0300_triad`, rebuilding from `records`:

1. Create `eventic_log`, `eventic_head`, `eventic_outbox`.
2. Copy `records` → `eventic_log`:
   - `stream := class_type`, `committed_at := created_ts`, `kind := 'create' if version = 0 else 'update'`
   - **Strip the phantom plugin keys** `seam`/`provides`/`requires`/`priority`/`mode`
     and the managed keys `id`/`version`/`version_id`/`created_ts` from `data`.
     Without this, `extra="forbid"` rejects your own historical rows on read — an easy
     thing to discover in production instead of here.
   - `snapshot := true` for old `FullSnapshot` rows; for old `DiffStorage` rows,
     `snapshot := (data->>'kind' = 'snapshot')` and unwrap `data->'state'` / `data->'patch'`
     into the new shapes (old deltas have no tombstones, which is correct — they never
     recorded removals).
3. Build `eventic_head` by replaying each stream (reuse `cli.py rebuild-heads`).
4. Drop `records`.

Write `downgrade()` honestly or not at all: a rebuild of `records` from `eventic_log` is
possible but cannot restore the phantom fields, and should not.

**Tests:** `test_migrations.py` seeds a realistic 0.2 database (both codecs, a
plugin-bearing class with phantom fields) and asserts the upgraded data reads back
correctly through the 0.3 API.

## Step 23 · Docs and examples

Rewrite `README.md` around the new surface; rewrite `MIGRATION.md` for 0.2 → 0.3;
rewrite `examples/demo.py` on `draft()`; make `examples/webhook.py` build its app in a
function. Promote `CONCEPT.md` (v3) out of `.scratch` to `docs/CONCEPT.md` — it is the
document the library is accountable to. Delete `PLUGINS.md`: the plugin framework it
describes no longer exists.

Verify every claim in the README against a test. Three of v2's README claims were
false (`use()`, per-class delivery, `created_ts`), and each was false because nothing
executed it.

## Step 24 · Final validation matrix

| Check | Command | Expect |
|---|---|---|
| Regression suite | `pytest src/tests/regression -q` | **0 xfailed, 24 passed** |
| Full suite | `pytest src/tests -q` | green |
| Warnings clean | `pytest src/tests -q -W error` | green |
| I6 — core is dependency-free | fresh interpreter import check | no `dbos` / `fastapi` |
| I8 — no process state | `test_no_reset_hooks.py` | **zero `_reset_*` in the package** |
| I5 — concurrency | `probes/probe_06` | 1 winner, 7 loud losers |
| Postgres | `pytest -m postgres` | green |
| Public surface | every README example | executed by a test |

Then re-run **all six original probes**. `probe_01`–`probe_05` should now report the
corrected behavior; `probe_06`'s concurrency section must be unchanged. Keep them in
`probes/` as the permanent record.

Bump to `0.3.0`.

---

## Sequencing rationale

**Why Phase 1 first.** The `UnitOfWork` is the only change that fixes a broken
invariant, and Phases 3 (outbox) and 4 (head projection) are both *rows written inside
its transaction*. Attempting either first means writing them twice.

**Why Phase 2 before Phase 3.** Subscriptions and dispatch are the parts most entangled
with the global delivery registry; deleting `plugins/` first removes the thing they
would otherwise have to interoperate with.

**Why value semantics (Phase 5) late.** `frozen=True` and `extra="forbid"` cause broad,
shallow test fallout. Landing them after the structure settles means fixing each test
once instead of twice.

**What must not regress, at any step.** The append-only kernel, deterministic `uuid5`
identity, and the optimistic lock. `probe_06` is the canary: **1 winner, 7 loud
losers**, every gate. That behavior is the one thing v2 got unambiguously right, and
the refactor's job is to keep it while making everything around it true.
