# Eventic — Step-by-Step Refactoring Guide

**Applies to:** all findings in `REVIEW.md` (C1–C6 critical, H1–H8 high, M1–M12 medium, L1–L12 low)
**Target stack:** Python ≥3.13 · Pydantic 2.11 · SQLAlchemy 2.0.43+ · **DBOS 2.29.0** (upgraded from 1.5.0)
**Working convention:** one commit per step; each step has an explicit *exit gate* (tests pass, acceptance criteria met) before moving on.

> **Why DBOS 2.29 first.** Eventic pins `dbos>=1.5.0` (June 2025); the latest is **2.29.0** (verified against PyPI). The 2.x line brings four things this refactor depends on:
> 1. **SQLite support** — DBOS now defaults to `sqlite:///{name}` when no database URL is given, and supports SQLite application databases. This makes the entire test suite runnable without Postgres (Step 2 depends on it).
> 2. **`system_database_url` / `application_database_url`** replace the deprecated `database_url` (Step 1.3).
> 3. **Dependency surface changed**: `fastapi`, `alembic`, `jsonpickle` were *removed* from DBOS's requirements; `sqlalchemy[asyncio]>=2.0.43` and `psycopg[binary]>=3.1` became direct deps. Eventic must now declare what it uses directly (Step 1.2 — without it, `import eventic` breaks after the upgrade).
> 4. **API compatibility**: everything Eventic touches today — `DBOS(config=TypedDict, fastapi=...)`, `DBOS.sql_session`, `Queue(name, concurrency)`, `queue.enqueue()`, `@DBOS.transaction/step/workflow`, `DBOS.launch()`, `DBOS.destroy()` — is unchanged in 2.29 (verified against the 2.29.0 wheel). The upgrade is therefore **low-risk mechanically but required** for the rest of the plan.

---

## Phase 0 — Safety net (do before any code changes)

| # | Action |
|---|---|
| 0.1 | `git status` must be clean. If not, commit or stash. |
| 0.2 | Add `.scratch/` to `.gitignore` (it currently is not ignored). |
| 0.3 | Capture a baseline: `git log --oneline -5` and record the working `dbos==1.5.0` behavior notes from `REVIEW.md` Appendix A. |
| 0.4 | Branch: `git checkout -b refactor/dbos-2x-and-core-fixes`. Every step below commits to this branch; `main` stays releasable. |

**Exit gate:** clean tree, scratch ignored, branch exists.

---

## Step 1 — Upgrade DBOS 1.5.0 → 2.29.0

### 1.1 Update `pyproject.toml`

**Before:**

```toml
requires-python = ">=3.13"
dependencies = [
    "confidantic",
    "dbos>=1.5.0",
    "psycopg2-binary>=2.9.10",
    "python-dotenv>=1.1.1",
]
[project.scripts]
eventic-example = "examples.demo:main"
start = "eventic.main:main"

[tool.uv.sources]
confidantic = { git = "https://github.com/Bullish-Design/confidantic.git" }
```

**After:**

```toml
requires-python = ">=3.13"
dependencies = [
    "dbos>=2.29.0,<3.0",
    "fastapi>=0.115.2",          # runtime.py imports it directly; dbos no longer pulls it in
    "sqlalchemy>=2.0.43",        # store.py/models.py import it directly
    "psycopg[binary]>=3.1",      # single driver; matches DBOS's internal psycopg3 engine
    "pydantic>=2.0",             # Record/PropertiesBase are pydantic models
    "python-dotenv>=1.1.1",
]

[project.optional-dependencies]
pg = []                          # pg is now the default path; kept for README compatibility (M1)
test = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
]

[project.scripts]
eventic-example = "eventic.examples.demo:main"   # fixed in Step 6 (M2)
```

Delete the `[tool.uv.sources]` block and the `confidantic` dependency (verified: it is imported nowhere in `src/`).

> **Rationale (H5/M1):** DBOS 2.29 rewrites every `postgresql://` URL to `postgresql+psycopg://` internally (`_datasource_postgres._make_url`, verified in the 2.29 wheel). Eventic must do the same normalization on its own engine (Step 5.2) so both sides use psycopg3 and `psycopg2-binary` can be dropped. `fastapi`, `sqlalchemy`, and `pydantic` must be declared explicitly because DBOS 2.29 no longer depends on `fastapi`/`alembic`, and `pydantic` is only in DBOS's optional `validation` extra.

### 1.2 Regenerate the lockfile

```bash
uv lock --upgrade-package dbos
uv sync
```

(If `uv` is unavailable: `pip install -e '.[test]' --upgrade dbos==2.29.0`.)

### 1.3 Update `Eventic.init` for the 2.x config (deprecation hygiene)

`runtime.py` currently passes `{"name": ..., "database_url": ...}`. `database_url` still works in 2.29 (deprecated). Switch to the new keys and add app-name validation (DBOS 2.29 enforces 3–30 lowercase chars):

```python
@classmethod
def init(cls, *, name: str, database_url: str, fastapi: Optional[FastAPI] = None, **extra_cfg: Any) -> "Eventic":
    if cls._singleton is not None:
        raise RuntimeError(
            "Eventic.init()/create_app() may only be called once per process; "
            "call Eventic.reset() first (see Step 5.3)."
        )
    cfg = {
        "name": name,
        "application_database_url": database_url,   # replaces deprecated database_url
        **extra_cfg,
    }
    cls._singleton = cls(config=cfg, fastapi=fastapi)
    cls._engine = create_engine(_normalize_db_url(database_url), pool_pre_ping=True, future=True)
    init_eventic(cls._engine)
    return cls._singleton
```

(Requires Step 5.3's `Eventic.reset()` to exist first — implement both in the same commit.)

### 1.4 Smoke-test the upgrade (before any other change)

Create `src/tests/smoke_import.py` temporarily, or just run:

```bash
.venv/bin/python -c "import eventic; print(eventic.__file__)"
.venv/bin/python -c "from eventic.runtime import Eventic; print(Eventic.__mro__[1].__name__)"
```

Both must succeed. Then commit: **`Step 1: upgrade dbos to 2.29, declare direct deps, drop confidantic/psycopg2`**.

**Exit gate:** `import eventic` and `import eventic.runtime` succeed; `uv.lock` resolves `dbos==2.29.0`; no `confidantic` in the lockfile.

---

## Step 2 — Test infrastructure (unblocks every later gate)

All remaining steps ship with tests; this step makes that possible.

### 2.1 Make the `records` model portable so tests run on SQLite

`src/eventic/persistence/models.py` — replace Postgres-only types with portable ones (Postgres still gets `JSONB` via variant):

```python
import uuid
import datetime as dt
from sqlalchemy import Column, DateTime, Integer, String, Uuid, UniqueConstraint, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import JSON
from sqlalchemy.orm import declarative_base

Base = declarative_base()

JSONB = JSON().with_variant(postgresql.JSONB(), "postgresql")  # portable JSONB

def now_utc() -> dt.datetime:
    return dt.datetime.now(tz=dt.timezone.utc)

class RecordRow(Base):
    __tablename__ = "records"
    __table_args__ = (UniqueConstraint("id", "version", name="uq_records_id_version"),)  # Step 4.1 (C6)

    version_id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    id = Column(Uuid(as_uuid=True), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    class_type = Column(String, nullable=False)
    created_ts = Column(DateTime(timezone=True), default=now_utc, nullable=False)
    properties = Column(JSONB, nullable=False, default=dict)   # L5/L6: non-null
    data = Column(JSONB, nullable=False)
```

> Verified: `sa.Uuid` and `sa.JSON().with_variant(JSONB, "postgresql")` are available in SQLAlchemy 2.0.41, and the generic JSON comparator implements `contains` on SQLite (so `find_by_properties` works on both dialects).

### 2.2 DBOS+SQLite test harness

`src/tests/conftest.py`:

```python
import pytest
from sqlalchemy import create_engine
from eventic import Eventic
from eventic.core.record import Record
from eventic.bootstrap import init_eventic

@pytest.fixture()
def eventic(tmp_path):
    """One fresh Eventic per test: SQLite app DB, SQLite system DB (DBOS 2.x default)."""
    db_url = f"sqlite:///{tmp_path / 'eventic.db'}"
    Eventic.init(name="eventic-test", database_url=db_url)
    Eventic.launch()
    yield Eventic.instance()
    Eventic.destroy()
    Eventic.reset()  # Step 5.3 — clears singleton + _store so the next test re-inits
```

> Note: DBOS 2.x defaults the *system* database to `sqlite:///{name}` when no `system_database_url` is given, so this fixture needs **no Postgres at all**. Add a `postgres` variant later (CI service) for the JSONB/GIN-specific assertions.

### 2.3 First tests (they must FAIL before the fixes)

- `src/tests/test_record.py::test_mutation_outside_transaction_persists` — reproduces C1.
- `src/tests/test_queues.py::test_public_method_does_not_raise` — reproduces C2.
- `src/tests/test_record.py::test_v0_row_persisted_on_construction` — reproduces C5.
- `src/tests/test_record.py::test_where_returns_records` — reproduces C4.

Run: `pytest src/tests -x`. **Expect failures** — that is the correct state; these become the regression tests for Steps 3–4.

**Exit gate:** pytest runs; the four tests above fail with the exact errors described in REVIEW.md (AssertionError / DBOSWorkflowFunctionNotFoundError / no rows / UUID list).

---

## Step 3 — Fix the P0 criticals (C1, C2, C3, C5)

### 3.1 C1 + H2 — One session strategy for reads and writes

Replace `RecordStore`'s split-brain (writes via `DBOS.sql_session`, reads via private sessions) with a single context manager that prefers the ambient DBOS transaction session and falls back to the store's own engine session.

`src/eventic/persistence/store.py`:

```python
from contextlib import contextmanager
from sqlalchemy.exc import IntegrityError

class RecordStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    @contextmanager
    def _session(self):
        """Ambient DBOS transaction session if inside one; otherwise our own
        short-lived session (committed on clean exit). Fixes C1 + H2."""
        try:
            ambient = DBOS.sql_session
        except AssertionError:
            ambient = None
        if ambient is not None:
            yield ambient          # DBOS owns commit/rollback
            return
        with Session(self.engine, future=True) as s:
            yield s
            s.commit()             # standalone path — skipped on exception

    def append(self, rec: "Record") -> None:
        row_vals = {
            "version_id": rec.version_id,
            "id": rec.id,
            "version": rec.version,
            "class_type": rec.__class__.__name__,
            "properties": rec.properties.model_dump(mode="json") if rec.properties else {},
            "data": rec.model_dump(mode="json"),
        }
        with self._session() as s:
            s.execute(insert(RecordRow).values(**row_vals))

    def latest(self, rec_id: uuid.UUID, class_type: str | None = None) -> Dict[str, Any]:
        """Latest committed data snapshot for rec_id (optionally of one class).
        Tie-breaks by version_id.desc() so identical version numbers resolve
        deterministically (C6)."""
        with self._session() as s:
            q = (
                select(RecordRow.data)
                .where(RecordRow.id == rec_id)
                .order_by(RecordRow.version.desc(), RecordRow.version_id.desc())
                .limit(1)
            )
            if class_type:
                q = q.where(RecordRow.class_type == class_type)
            row = s.execute(q).first()
            return row.data if row else {}

    def stream(self, rec_id: uuid.UUID, class_type: str | None = None):
        """Yield rows oldest->newest, optionally filtered by class_type (H4)."""
        with self._session() as s:
            q = (
                select(RecordRow)
                .where(RecordRow.id == rec_id)
                .order_by(RecordRow.version)
            )
            if class_type:
                q = q.where(RecordRow.class_type == class_type)
            yield from (row for (row,) in s.execute(q))
```

> `stream()` and `find_by_properties()` move to the same `_session()` (stream shown above; `find_by_properties` in Step 4.2). Delete the duplicated `latest_sync`/`stream_sync` (M11) and the dead comment blocks. Because reads now share the transaction session, a `hydrate -> mutate -> hydrate` sequence inside one DBOS transaction sees its own writes (H2 fixed).

**Tests:** `test_mutation_outside_transaction_persists` now passes; add `test_read_your_own_write_inside_transaction` (H2).

### 3.2 C2 + C3 + M7 — Redesign the `evented` mechanism

Replace the blanket metaclass wrapping with an **explicit opt-in** decorator that (a) never wraps anything by default, (b) never double-executes, and (c) only ever enqueues a **DBOS-registered** function.

`src/eventic/queues/dispatcher.py` (new design):

```python
"""Opt-in queue decorator.

Semantics (no more "run now AND later" — that was at-least-twice execution):
* A method marked @evented is NOT run inline. It is scheduled on the per-class
  queue and executes as a DBOS workflow on a serialized *snapshot* of self.
* For aggregate mutations, prefer passing self.id and re-hydrating inside the
  step, so the queued run observes fresh state.
"""
from functools import wraps
from typing import Callable, Optional
from dbos import DBOS


def evented(fn: Optional[Callable] = None):
    """Explicit opt-in: schedule this method on the class queue."""
    if fn is None:                       # @evented with no parens
        return lambda f: _mark(f)

    # called as a bare decorator inside the class body: mark for the metaclass
    return _mark(fn)


def _mark(fn):
    fn.__eventic_evented__ = True        # discovered by RecordMeta
    return fn


def _queue_method(fn):
    """Metaclass hook: register fn as a DBOS step and return a scheduling wrapper."""
    step = DBOS.step()(fn)               # registers fn -> get_func_info succeeds (C2)
    @wraps(fn)
    def inner(self, *args, **kwargs):
        return self.__class__.queue.enqueue(step, self, *args, **kwargs)
    return inner
```

`src/eventic/core/record.py` — `RecordMeta.__new__` becomes conservative:

```python
def __new__(mcls, name, bases, ns, **kw):
    cls = super().__new__(mcls, name, bases, ns)
    if name == "Record":
        return cls

    cls._queue_name = f"queue_{_snake(name)}"
    cls.queue = Queue(cls._queue_name, concurrency=1)   # single declaration (M7)

    from eventic.queues.dispatcher import _queue_method, _mark

    for attr, fn in ns.items():
        if getattr(fn, "__eventic_evented__", False):   # ONLY explicitly marked
            setattr(cls, attr, _queue_method(fn))
        # everything else — including staticmethods, classmethods, properties,
        # and any model_* internals — is left completely untouched (C3)

    return cls
```

> **C2 fixed:** the enqueue target is now `DBOS.step()`-registered, so `start_workflow`'s `get_func_info` check succeeds; no more `DBOSWorkflowFunctionNotFoundError` after execution. No more synchronous+queued double execution.
> **C3 fixed:** `staticmethod`/`classmethod`/`property`/`model_*` are never wrapped.
> **M7 fixed:** exactly one `Queue` per class (`cls.queue`); `_queue_method` reuses it.
> **Behavior change (documented):** `@evented` methods are *scheduled*, not run inline. Update `README.md` accordingly (Step 8). The demo's `snapshot`-style steps should use the queue directly (`Story.queue.enqueue(...)`), which already works.

**Tests:** `test_public_method_does_not_raise` (a plain public method now runs inline, no exception); `test_staticmethod_untouched`; `test_evented_schedules_without_inline_run` (asserts DBOS workflow is created and the method is *not* executed synchronously); `test_no_duplicate_queue_declarations` (no `has already been declared` warning via `caplog`).

### 3.3 C5 — Persist version 0 at construction

`src/eventic/core/record.py` — `model_post_init`:

```python
def model_post_init(self, _ctx):
    is_new = self.id is None
    if self.id is None:
        object.__setattr__(self, "id", uuid.uuid4())
    if self.properties is None:
        object.__setattr__(self, "properties", PropertiesBase(record_type=self.__class__.__name__))
        self.properties._bind(self)              # H1 — see Step 5.1
    elif self.properties.record_type == "":
        object.__setattr__(self.properties, "record_type", self.__class__.__name__)
        self.properties._bind(self)

    if is_new and self.version == 0:
        from eventic.events import emit_create
        if self._store is not None:
            self._store.append(self)             # durable v0 — the row now exists
        emit_create(self)                        # handlers can hydrate (H6 timing)
```

> No recursion: every later reconstruction (`__setattr__`'s `new_obj`, `hydrate`'s `model_validate`) carries a non-None `id`, so `is_new` is `False` and nothing is appended. Outside a DBOS transaction the append now works via the Step 3.1 fallback (C1) — so construction writes even in plain scripts. If the store is not wired yet (record created before `Eventic.init`), the append is skipped and the create event still fires; document that the row lands on first mutation in that case.

**Tests:** `test_v0_row_persisted_on_construction`; `test_construct_then_hydrate_roundtrip`; `test_create_event_fires_after_persist` (handler hydrates successfully).

**Commit:** `Step 3: fix C1/C2/C3/C5 — session fallback, opt-in evented, persist v0`.

**Exit gate:** the four Step-2.3 tests pass; no `evented`-related exceptions anywhere in the suite.

---

## Step 4 — Fix the P1 data-integrity issues (C6, C4, H4)

### 4.1 C6 — Deterministic versioning + uniqueness

1. **Unique constraint** (already added to the model in Step 2.1).
2. **Deterministic `version_id`** so crash-recovery replays are idempotent (`record.py` `__setattr__`):

```python
data = self.model_dump(mode="python")
data[name] = value
data["version"] = self.version + 1
# Deterministic version_id: identical (id, version) replays collide on PK and
# are safely ignored, instead of inserting duplicate rows (C6).
data["version_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"eventic:{self.id}:{data['version']}"))
new_obj = self.__class__(**data)
self._ensure_store()
self._store.append(new_obj)
```

3. **Tolerate identical replays** in `append` (works on both Postgres and SQLite in SQLAlchemy 2.0):

```python
with self._session() as s:
    s.execute(insert(RecordRow).values(**row_vals).on_conflict_do_nothing())
```

> **How correctness is preserved:** the deterministic `version_id` + `UNIQUE(id, version)` + `ON CONFLICT DO NOTHING` makes a DBOS crash-recovery re-run of a transaction insert the *same* row (no-op) rather than a duplicate. For two *genuinely different* concurrent writers, the same `(id, version)` still collides — with DBOS transactions at the 2.29 default `SERIALIZABLE` isolation, the loser aborts with a serialization error and **DBOS retries the whole transaction**, re-hydrating and bumping to the next free version. In standalone scripts the collision surfaces as `IntegrityError` instead of silent corruption — retry or serialize per aggregate (e.g., the per-class `concurrency=1` queue).
4. `latest()` tie-break by `version_id.desc()` (already in Step 3.1).

**Tests:** `test_concurrent_mutations_do_not_duplicate_versions` (two threads/transactions); `test_replayed_append_is_idempotent` (same `(id, version)` appended twice → one row).

### 4.2 C4 + H4 — `where()` returns records; every read filters by `class_type`

`store.py`:

```python
def find_by_properties(self, class_type: str, filter_: Dict[str, Any]) -> List[uuid.UUID]:
    latest = (
        select(RecordRow.id.label("rid"), RecordRow.properties.label("props"))
        .where(RecordRow.class_type == class_type)          # H4
        .distinct(RecordRow.id)
        .order_by(RecordRow.id, RecordRow.version.desc())
    ).subquery()
    with self._session() as s:
        q = select(latest.c.rid).where(latest.c.props.contains(filter_))
        return [rid for (rid,) in s.execute(q)]
```

Pass `class_type` from `latest()`/`stream()` too (Step 3.1 signature already accepts it) and thread it through `hydrate`.

`record.py`:

```python
@classmethod
def where(cls: type[T_Record], **filters: Any) -> list[T_Record]:
    """Return hydrated records whose JSONB properties match all key/value pairs."""
    cls._ensure_store()
    ids = cls._store.find_by_properties(cls.__name__, filters)
    return [cls.hydrate(rid) for rid in ids]               # C4: real records, not UUIDs

@classmethod
def hydrate(cls, rec_id, at_version=None):
    cls._ensure_store()
    if at_version is None:
        state = cls._store.latest(rec_id, class_type=cls.__name__)
        if not state:
            raise KeyError(f"{cls.__name__} {rec_id} not found (no committed rows yet)")
        return cls.model_validate(state)
    obj = None
    for row in cls._store.stream(rec_id, class_type=cls.__name__):
        if row.version > at_version:
            break
        obj = cls.model_validate(row.data)
    if obj is None:
        raise KeyError(f"{cls.__name__} {rec_id} <= v{at_version} not found")
    return obj
```

> **H8 (signature clarity):** rename the `version` parameter to `at_version`, keep the "latest ≤ at_version" semantics but document it explicitly in the docstring. **H4 caveat to document:** `class_type` matches `cls.__name__` exactly; if you subclass records, register/query the subclass name (or extend `_class_type` matching over the subclass registry — out of scope here).

**M4 (filter normalization)** in `find_by_properties` — JSONB `contains` needs JSON-serializable values:

```python
import datetime as dt
def _jsonable(value):
    if isinstance(value, uuid.UUID): return str(value)
    if isinstance(value, (dt.date, dt.datetime)): return value.isoformat()
    return value

filter_ = {k: _jsonable(v) for k, v in filter_.items()}
```

**Tests:** `test_where_returns_records`; `test_where_filters_by_class_type` (same properties on two classes); `test_hydrate_wrong_class_raises`; `test_hydrate_at_version`.

**Commit:** `Step 4: fix C6/C4/H4 — unique versions, real where(), class_type isolation`.

**Exit gate:** all data-integrity tests pass on SQLite; `pytest -q` green except any explicitly-postponed Postgres-only tests.

---

## Step 5 — Fix the P2 issues (H1, H3, H5, H6, H7, H8)

### 5.1 H1 — `PropertiesBase` mutations persist automatically

`src/eventic/core/properties.py`:

```python
from typing import Any, Dict, Optional
from pydantic import BaseModel, PrivateAttr

class PropertiesBase(BaseModel):
    record_type: str = ""
    model_config = {"extra": "allow", "frozen": False, "arbitrary_types_allowed": True}

    _owner: Optional["Record"] = PrivateAttr(default=None)

    def _bind(self, owner) -> None:
        object.__setattr__(self, "_owner", owner)

    def add(self, **kv: Any) -> None:
        for k, v in kv.items():
            setattr(self, k, v)
        self._persist()

    def remove(self, key: str) -> None:
        if hasattr(self, key):
            delattr(self, key)
            self._persist()

    def _persist(self) -> None:
        owner = self._owner
        if owner is not None:
            owner.__setattr__("properties", self)   # version bump + append (reuses C1 session)

    def list(self) -> Dict[str, Any]:
        return self.model_dump()
```

Binding happens in `Record.model_post_init` (Step 3.3 already calls `_bind`). The `props.add(x=y)` call now writes a new version; the demo's `s.properties = s.properties` hack is deleted. Note `record_type` is intentionally settable only via the owner.

**Tests:** `test_properties_add_persists_new_version`; `test_properties_remove_persists`; `test_detached_properties_do_not_write`.

### 5.2 H5 — One engine, one driver

`runtime.py` URL normalization helper (mirrors DBOS's own `_make_url`):

```python
from sqlalchemy.engine import make_url

def _normalize_db_url(url: str) -> str:
    u = make_url(url)
    if u.drivername.startswith("postgresql") and u.drivername != "postgresql+psycopg":
        u = u.set(drivername="postgresql+psycopg")
    return str(u)
```

Use `_normalize_db_url(database_url)` for `create_engine` (Step 1.3). `psycopg2-binary` was already removed in Step 1 — verify `pip check` is clean.

### 5.3 H3 — Lifecycle: explicit re-init/reset, no silent no-ops

`runtime.py`:

```python
@classmethod
def reset(cls) -> None:
    """Tear down the singleton so the next init() starts fresh (tests, multi-app)."""
    if cls._singleton is not None:
        DBOS.destroy()                 # 2.29 signature: destroy(workflow_completion_timeout_sec=...)
    cls._singleton = None
    cls._engine = None
    Record._store = None               # subclasses inherit None via class lookup

@classmethod
def instance(cls) -> "Eventic":
    if cls._singleton is None:
        raise RuntimeError("Eventic.init() has not been called")
    return cls._singleton
```

Second `init()`/`create_app()` calls now raise (Step 1.3) instead of silently returning an unwired app. `conftest.py` (Step 2.2) already calls `Eventic.reset()` between tests.

**Tests:** `test_second_init_raises`; `test_reset_allows_reinit`; `test_create_app_returns_wired_app`.

### 5.4 H6 — Event registry: class-object keys, isolation, deterministic order

`src/eventic/events.py`:

```python
import logging
from collections import defaultdict
from typing import Callable, Dict, List, Set, Type

logger = logging.getLogger(__name__)

class EventRegistry:
    def __init__(self):
        # keyed by the CLASS OBJECT (two "Story" classes in different modules
        # no longer cross-fire), ordered list preserves registration order.
        self._handlers: Dict[str, Dict[type, List[Callable]]] = {
            "create": defaultdict(list),
            "update": defaultdict(list),
        }

    def register(self, event_type: str, record_classes: tuple, handler: Callable) -> None:
        for cls in record_classes:
            self._handlers[event_type][cls].append(handler)

    def emit(self, event_type: str, instance) -> None:
        for cls in instance.__class__.__mro__:
            for handler in self._handlers[event_type].get(cls, []):
                try:
                    handler(instance)
                except Exception:
                    # Isolation policy: a failing handler must not break the
                    # mutation/construction that emitted the event.
                    logger.exception("event handler %s failed for %s(%s)",
                                     handler.__name__, instance.__class__.__name__, instance.id)
```

Remove `_class_map` (dead code, M11). Update `emit_create`/`emit_update` call sites unchanged. Timing note: create fires *after* the v0 append (Step 3.3); update fires after the append but before commit — document in the module docstring that handlers should treat the store as eventually-consistent within the emitting transaction.

**Tests:** `test_handlers_keyed_by_class_object`; `test_failing_handler_does_not_break_mutation`; `test_handler_order_is_registration_order`.

### 5.5 H7 — Reflect *validated* state locally (no divergence)

`record.py` `__setattr__` — reflect everything from `new_obj`, not just the raw assignment:

```python
new_obj = self.__class__(**data)          # validates the WHOLE model (H7)
self._ensure_store()
self._store.append(new_obj)

# Reflect the validated state so local == persisted (fixes H7 divergence;
# also means an invalid assignment raises ValidationError *before* any
# partial local mutation).
for field, val in new_obj.model_dump(mode="python").items():
    object.__setattr__(self, field, val)
object.__setattr__(self, "version", new_obj.version)
object.__setattr__(self, "version_id", new_obj.version_id)
```

Also add a **no-op guard** (L4) to avoid write amplification: if `self.model_dump(mode="python") == new_obj.model_dump(mode="python")` (ignoring `version`/`version_id`), return early without appending.

**Tests:** `test_local_state_matches_persisted_state_after_coercion` (e.g., `int`→`bool`, `float`→`int` fields); `test_noop_assignment_does_not_create_version`.

### 5.6 M9 — `__setattr__` special names

Explicit, documented handling in `record.py`:

```python
def __setattr__(self, name: str, value: Any):
    if name.startswith("_"):
        object.__setattr__(self, name, value)          # private attrs: plain set
        return
    if name in {"version", "version_id"}:
        raise AttributeError(
            f"{name} is derived from aggregate history and cannot be assigned directly")
    if name == "id":
        raise AttributeError("id is the aggregate identity; it cannot be reassigned")
    ...  # normal copy-on-write path
```

**Commit:** `Step 5: fix H1/H3/H5/H6/H7/M9 — property persistence, lifecycle, events, validated reflection`.

**Exit gate:** full suite green on SQLite.

---

## Step 6 — Packaging & configuration (M1, M2, M3, M5, M8, M10)

### 6.1 M2 — Move examples under the `eventic` package

```bash
git mv src/examples src/eventic/examples
# fix relative imports inside eventic/examples/demo.py; keep the module importable as eventic.examples.demo
```

Update `pyproject.toml` hatch packages:

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/eventic"]
```

Now `python -m eventic.examples.demo` (README) works and the global `examples` namespace is no longer polluted.

### 6.2 M3 — Rewrite `dbos-config.yaml` for the DBOS 2.29 schema

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/dbos-inc/dbos-transact-py/main/dbos/dbos-config.schema.json
name: eventic
language: python
database_url: ${DBOS_DATABASE_URL}
runtimeConfig:
  start:
    - "uvicorn eventic.main:app --host 0.0.0.0 --port 8000"
  migrate:
    - "alembic upgrade head"
```

> Verified against the 2.29 schema: top-level keys are `name, language, database_url, system_database_url, database(app_db_name, migrate), runtimeConfig, env`. The old `database: {hostname, port, username, password, app_db_name}` block is invalid in both 1.5 and 2.29 and must go. `main.py` must expose an `app` object (it currently only has `main()`; add `app = Eventic.create_app(...)` at module level).

### 6.3 M5 — Real Alembic migrations for `records`

Declare `alembic>=1.13` in `pyproject.toml` (DBOS 2.29 no longer pulls it). Then:

```bash
mkdir -p migrations
alembic init migrations          # generates migrations/env.py + alembic.ini wiring
```

- Point `alembic.ini` `script_location = %(here)s/migrations`.
- `migrations/env.py`: `target_metadata = RecordRow.metadata` (import from `eventic.persistence.models`).
- Generate the initial revision: `alembic revision --autogenerate -m "initial records table"` and review it (it must contain the `records` table with the `uq_records_id_version` constraint).
- `bootstrap.init_eventic` — replace `Base.metadata.create_all(engine)` with a documented check: create the table if missing for dev ergonomics, but treat Alembic as the source of truth in production (env flag, e.g. `EVENTIC_AUTO_CREATE_TABLES`).

### 6.4 M8 — Export `init_eventic`

`src/eventic/__init__.py`:

```python
from .bootstrap import init_eventic
__all__ = ["Eventic", "Record", "PropertiesBase", "on", "init_eventic"]
```

Update `_ensure_store`'s error message to mention both `Eventic.init()` and `init_eventic(engine)`.

### 6.5 M10 — Clean `Eventic.queue()`

```python
@classmethod
def queue(cls, name: str, *, concurrency: int | None = None, **kw):
    """Create/reuse a DBOS Queue. concurrency=None means DBOS's default."""
    return Queue(name, concurrency=concurrency, **kw)
```

Delete the commented-out allowlist block.

**Commit:** `Step 6: packaging & config — eventic.examples, dbos-config, alembic, exports`.

**Exit gate:** `python -m eventic.examples.demo` imports; `alembic upgrade head` runs against a scratch DB and creates `records`; `dbos-config.yaml` passes `jsonschema` validation against the 2.29 schema.

---

## Step 7 — Application examples & webhook hardening (M6, M2 leftovers)

### 7.1 Rewrite `src/eventic/main.py` webhook

- Add module-level `app = Eventic.create_app(...)`.
- Validate the body against a strict input schema; never pass attacker-controlled `id`/`version`/`version_id`/`properties` into `Story`:

```python
from pydantic import BaseModel, Field

class WebhookPayload(BaseModel):
    title: str | None = None
    body: str | None = None
    # NO version/id/properties fields — they are reserved (M6)

@app.post("/webhook")
async def webhook(payload: WebhookPayload):
    story = Story(title=payload.title, body=payload.body)   # v0 auto-persisted (C5)
    # ... logging uses story.model_dump_json()
    return {"status": "logged", "id": str(story.id)}
```

- Replace `datetime.utcnow()` with `datetime.now(timezone.utc)`; drop the unused `on` import; use FastAPI's request-body validation instead of `await request.json()`.

### 7.2 Simplify `src/eventic/examples/demo.py`

- Delete the `Eventic.init(...)` call inside `main()` (it was a guaranteed no-op after `create_app`, H3/L7); keep `Eventic.launch()`.
- Replace the `s.properties = s.properties` and `props.add(...)` dance with plain `s.properties.add(status="published")` (H1 now handles it).
- Remove the `# story_queue = Eventic.queue(...)` dead comments.

**Tests:** `test_webhook_persists_and_rejects_metadata_injection` (post body containing `version=99` → ignored/rejected).

---

## Step 8 — Documentation & final validation

### 8.1 Rewrite the broken README claims

- Minimal-script section: now correct *because* C1 is fixed (mutation outside a transaction persists via the standalone session), but update the prose to state the DBOS-transaction fast path.
- Replace the "methods run now *and* later" feature row with the new `@evented` semantics ("opt-in scheduling; no double execution").
- Fix the install command (`pip install eventic`; the `[pg]` extra is now a no-op alias for README compatibility).
- Fix `python -m eventic.examples.demo`.
- Document the `records` schema, the unique `(id, version)` constraint, deterministic `version_id`, and the concurrency contract (DBOS SERIALIZABLE retry / IntegrityError in scripts).
- Document the event-handler isolation policy and the "handlers see pre-commit state within the emitting transaction" caveat.

### 8.2 Data migration for existing deployments (C6 backfill)

Existing tables lack the unique constraint and may contain duplicate `(id, version)` rows:

```sql
-- 1. dedupe: keep the latest version_id per (id, version)
DELETE FROM records a USING records b
WHERE a.version_id <> b.version_id
  AND a.id = b.id AND a.version = b.version
  AND a.version_id < b.version_id;

-- 2. add the constraint
ALTER TABLE records ADD CONSTRAINT uq_records_id_version UNIQUE (id, version);
```

(Wrap in an Alembic migration `migrations/versions/xxxx_records_unique_version.py`.)

### 8.3 Full verification matrix

| Check | Command |
|---|---|
| Import | `.venv/bin/python -c "import eventic, eventic.runtime, eventic.examples.demo"` |
| Unit suite (SQLite) | `pytest src/tests -q` |
| Postgres integration (CI) | `pytest -m postgres` against a PG service (`database_url=postgresql+psycopg://…`) |
| Deps sanity | `pip check`; `grep -c confidantic uv.lock` == 0 |
| Config schema | validate `dbos-config.yaml` against the 2.29 JSON schema |
| Alembic | `alembic upgrade head && alembic downgrade base` on a scratch DB |
| No queue-duplicate warnings | `pytest -q -W error` / assert `caplog` has no `already been declared` |

**Exit gate:** every row of the matrix passes; `git log` shows one commit per Step 1–8.

---

## Rollback plan

- Each step is an isolated commit; revert by step (`git revert`), since no step depends on later ones except Step 1.3↔5.3 (implement together or accept a temporary single-singleton warning).
- If the DBOS 2.29 upgrade itself is blocked, Steps 3–5 are still compatible with DBOS 1.5.0 *except*: SQLite tests (Step 2) need 2.x; `system_database_url`/`application_database_url` and `DBOS.destroy()` timeout param are 2.x-only. In that case, use the Postgres-only variant of the test harness and keep `database_url`.
- Data safety: the unique constraint migration is additive and reversible; the dedupe `DELETE` must be reviewed in a staging DB first.

---

## Appendix — Finding → Step map

| Finding | Fixed in |
|---|---|
| C1 mutation outside transaction | Step 3.1 |
| C2 evented always raises / double-exec | Step 3.2 |
| C3 staticmethods destroyed | Step 3.2 |
| C4 where() returns UUIDs | Step 4.2 |
| C5 v0 never persisted | Step 3.3 |
| C6 concurrency/duplicate versions | Step 4.1 (+ migration 8.2) |
| H1 properties silent loss | Step 5.1 |
| H2 non-transactional reads | Step 3.1 |
| H3 singleton silent no-op | Steps 1.3, 5.3 |
| H4 no class_type filter | Step 4.2 |
| H5 dual engines/drivers | Steps 1.1, 5.2 |
| H6 event registry flaws | Step 5.4 |
| H7 local/persisted divergence | Step 5.5 |
| H8 hydrate version semantics | Step 4.2 |
| M1 `eventic[pg]` broken | Step 1.1 |
| M2 `eventic.examples` broken | Step 6.1 |
| M3 dbos-config broken | Step 6.2 |
| M4 JSONB filter normalization | Step 4.2 |
| M5 no migrations | Step 6.3 |
| M6 webhook injection | Step 7.1 |
| M7 duplicate queue declarations | Step 3.2 |
| M8 init_eventic not exported | Step 6.4 |
| M9 special-attr handling | Step 5.6 |
| M10 dead whitelist | Step 6.5 |
| M11 dead/duplicate store code | Steps 3.1, 5.4 |
| M12 no tests | Step 2 |
| L1 ClassVar import | Step 3.x (add `ClassVar` to `typing` imports in record.py) |
| L2 dump-mode asymmetry | Step 5.5 (documented choice: keep `mode="python"` for `__setattr__`, `mode="json"` at the store boundary) |
| L3 `_snake` edge cases | Step 8.1 (document + validation) |
| L4 no-op write churn | Step 5.5 |
| L5 properties column nullable | Step 2.1 |
| L6 data/properties duplication | documented in Step 8.1 (kept for query performance) |
| L7 demo init no-op | Step 7.2 |
| L8 launch double path | Step 7.2 + README |
| L9 console script naming | Step 1.1 |
| L10 webhook error handling | Step 7.1 |
| L11 dbos-config schema drift | Step 6.2 |
| L12 README enshrines double-exec | Step 8.1 |

---

## Notes on claims verified against DBOS 2.29.0 (wheel, `pip` metadata)

- `DBOS(config=…)` still accepts a `DBOSConfig` TypedDict; `translate_dbos_config_to_config_file` still handles `name` + `database_url`, and now also `system_database_url`/`application_database_url`.
- `DBOS.sql_session` is unchanged (asserts transaction context) → the Step 3.1 fallback is required on 2.29 exactly as on 1.5.
- `Queue(name, concurrency=…)` + `queue.enqueue(func, *args)` unchanged; `start_workflow` still raises `DBOSWorkflowFunctionNotFoundError` for unregistered functions → Step 3.2's registration is required.
- `@DBOS.transaction()` defaults to `SERIALIZABLE` in 2.29 → underpins the Step 4.1 concurrency contract.
- Default serializer is now `pickle`-based (jsonpickle removed) → `Record` instances are serializable as workflow args, but the "snapshot semantics" note in Step 3.2 stands.
- `DBOS.launch()` lost the `debug_mode` parameter (Eventic never used it); `DBOS.destroy(workflow_completion_timeout_sec=…)` gained a parameter.
- DBOS 2.29 rewrites `postgresql://` → `postgresql+psycopg://` internally; SQLite system DB is the default when no URL is provided.
