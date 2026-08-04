# Eventic — Adversarial Code Review

**Review date:** 2026-08-04
**Version reviewed:** `0.1.5` (git `HEAD` a65ac96), Python 3.13, DBOS 1.5.0, Pydantic 2.11.7, SQLAlchemy 2.0.41
**Scope:** all of `src/eventic/`, `src/examples/`, `pyproject.toml`, `dbos-config.yaml`, `alembic.ini`, `README.md`
**Method:** line-by-line source review, cross-checked against the installed DBOS 1.5.0 implementation (`dbos/_dbos.py`, `dbos/_core.py`, `dbos/_queue.py`, `dbos/_dbos_config.py`, `dbos/_context.py`), plus live execution probes (see Appendix A) for every claim marked **[verified]**.

---

## 1. Executive summary

Eventic is a promising idea — copy-on-write, versioned Pydantic aggregates persisted to a single Postgres table, driven by DBOS durable queues — but in its current state the **headline features are broken or crash the process**:

1. **Every attribute mutation crashes outside a DBOS transaction.** `Record.__setattr__` → `RecordStore.append` → `DBOS.sql_session` raises `AssertionError` unless the call happens inside `@Eventic.transaction()`. The README's minimal example (`item.text = "Ship v1"` in a plain script) fails. **[verified]**
2. **Every public method on a `Record` subclass raises after executing.** The metaclass wraps methods with `evented`, which runs the method synchronously (side effects happen!) and then calls `q.enqueue(fn, …)`. DBOS raises `DBOSWorkflowFunctionNotFoundError` because the raw method was never registered, or `DBOSException("No DBOS was created yet")` before init. **[verified]**
3. **User `@staticmethod`s are destroyed by the metaclass** (wrapped into the same broken decorator). `Story.ping(5)` raises. **[verified]**
4. **`Record.where()` returns a list of UUIDs, not records**, contradicting its own signature, docstring, and README usage.
5. **Version-0 rows are never persisted automatically**; the flagship `main.py` webhook creates a `Story` and silently never stores it. `emit_create` fires for records that are never persisted (ghost events).
6. **No concurrency control on version increments** — two concurrent writers both write version N+1; there is no unique `(id, version)` constraint and no optimistic lock, so aggregate history corrupts under load.

There is **zero test coverage** (`src/tests/` is an empty `__init__.py` and isn't even shipped in the wheel). Several advertised capabilities (Alembic migrations, `pip install eventic[pg]`, `python -m eventic.examples.demo`) are broken by packaging/config errors.

Severity tallies: **6 critical, 8 high, 12 medium, ~10 low**. Findings are ordered by severity, not file order.

---

## 2. CRITICAL

### C1. Copy-on-write mutation only works inside a DBOS transaction — the core feature crashes in plain scripts

`src/eventic/core/record.py:90-112` (`Record.__setattr__`) → `src/eventic/persistence/store.py:86`:

```python
DBOS.sql_session.execute(insert(RecordRow).values(**row_vals))
```

DBOS 1.5.0 (`dbos/_dbos.py:1079-1087`):

```python
@classproperty
def sql_session(cls) -> Session:
    ctx = assert_current_dbos_context()
    assert ctx.is_transaction(), "db is only available within a transaction."
```

`assert_current_dbos_context()` (`dbos/_context.py:271-274`) asserts *"No DBOS context found"* when called outside any DBOS operation. Consequences:

- Plain script (`README.md` "minimal script"): `item.text = "Ship v1"` → `AssertionError: No DBOS context found`.
- Inside a DBOS **step** (not transaction): `AssertionError: db is only available within a transaction.`
- Only inside `@Eventic.transaction()` does it work.

The code comment (`store.py:83-91`) shows an intended standalone fallback (`with Session(self.engine, future=True)…`), but it is **commented out** — the exact "minimal script, no web server" use case the README markets is dead code. `dbos-config.yaml` even tells users to run `dbos-runner`/scripts without DBOS wrappers.

**Fix:** restore the engine-session fallback in `append()` (or better: always use the store's own `Session` for `records` writes and only use DBOS's ambient session when present), and add a regression test for out-of-context mutation.

### C2. The `evented` metaclass wrapper always raises after executing the method (and would double-execute if it didn't)

`src/eventic/queues/dispatcher.py:14-29`:

```python
def inner(self: "Record", *args, **kwargs):
    result = fn(self, *args, **kwargs)          # side effects happen NOW
    q.enqueue(fn, self, *args, **kwargs)        # then this ALWAYS raises
    return result
```

`Queue.enqueue` → `start_workflow` (`dbos/_core.py:start_workflow`):

```python
fi = get_func_info(func)
if fi is None:
    raise DBOSWorkflowFunctionNotFoundError(
        "<NONE>", f"start_workflow: function {func.__name__} is not registered")
```

The raw method `fn` is never decorated with `@DBOS.step`/`@DBOS.workflow`, so `get_func_info(fn)` is `None`. Before `Eventic.init()`, `_get_dbos_instance()` raises `DBOSException("No DBOS was created yet")` even earlier. **[verified]** Both cases occur *after* the synchronous execution.

Result: **any public method on a Record subclass executes its side effects (including DB writes) and then raises an exception to the caller.** The demo only avoids this because `Story` defines no public methods (only `_format_story`, which is underscore-prefixed). And even if the registration check were fixed, the design runs every method twice (once sync, once queued) — at-least-twice execution of arbitrary user methods, a correctness disaster for any non-idempotent method.

Additional wrinkle: `q.enqueue(fn, self, …)` serializes the *whole `Record` instance* (jsonpickle) as a workflow argument, including its store-coupled `PropertiesBase`; and the queued invocation would call the method on a deserialized copy, not the live object — so "later" execution would operate on a stale snapshot, not current state.

**Fix:** wrap the synchronous call only (drop the enqueue), or enqueue only *explicitly* opted-in methods with real DBOS-registered step functions; never blanket-wrap every public method. Document the semantics. The current behavior violates the principle of least surprise in the worst way (side effect + exception).

### C3. The metaclass destroys user `@staticmethod`s (and is one `callable()` quirk away from destroying more)

`src/eventic/core/record.py:50`:

```python
if callable(fn) and not attr.startswith("_") and attr != "model_post_init":
    setattr(cls, attr, evented(queue_name)(fn))
```

In Python 3.13, `callable(staticmethod(f))` is `True` (verified: `staticmethod.__call__` exists), while `callable(classmethod(f))` is `False`. So:

- `@staticmethod` methods are wrapped by `evented` → `Story.ping(5)` raises `DBOSException` (C2). **[verified]**
- `@classmethod` methods happen to survive — because of an implementation detail of `staticmethod`, not by design.

This is a silent behavioral fork based on decorator type. Also note the blanket wrapping would also grab any pydantic-internal method a subclass overrides (`model_dump`, `model_copy`, `model_dump_json`, …), which would inject the broken enqueue into FastAPI's response serialization path and pydantic's own `__setattr__` machinery.

**Fix:** only wrap a curated allowlist (or explicit `@evented` opt-in decorator); skip `staticmethod`/`classmethod`/`property` objects explicitly; never wrap `model_*` methods.

### C4. `Record.where()` returns UUIDs but claims to return records

`src/eventic/core/record.py:115-121`:

```python
@classmethod
def where(cls: type[T_Record], **filters: Any) -> list[T_Record]:
    """Return records whose JSONB properties match all of the given key/value pairs.
    Example: Story.where(status="published", audience="kids")"""
    return cls._store.find_by_properties(filters)
```

`find_by_properties` (`store.py:116-128`) returns `List[uuid.UUID]` — a list of *ids*. Nothing hydrates them. So `Story.where(status="published")` gives the caller `[UUID, …]` while the type signature, docstring, and README ("Return records whose JSONB properties match…") promise `Story` instances. Downstream code doing `s.title` will crash with `AttributeError` at runtime; the failure is silent at the API boundary.

**Fix:** either hydrate each id (`cls.hydrate(rid)`), rename the method (`where_ids` / `find_ids`), or change the return annotation. Also add the missing `class_type` filter (see H4).

### C5. Version-0 rows are never persisted; the flagship example silently loses every record

`Record.__setattr__` appends **only the new version** (`record.py:96-102`). A freshly constructed `Record` is never written unless the caller explicitly calls `Story._store.append(s0)` — which only `examples/demo.py`'s `create_story` does. Consequences:

- `src/eventic/main.py` webhook: `story = Story(**body)` — the record is **never persisted**. The example's entire purpose (store the webhook payload as a versioned aggregate) is silently dropped. The handler then returns "status: logged" as if it succeeded.
- `README.md` minimal script: after `item.text = "Ship v1"`, only the v1 row exists; history claims v0 exists but `hydrate(item.id)`'s `latest` returns v1 and `stream()` has no v0. History is inconsistent with the versioning contract.
- `emit_create` fires in `model_post_init` (record.py:85-87) at construction time, *before any append* — so create handlers observe (and may try to `hydrate`) records that have no committed rows. Ghost events.

**Fix:** append the initial version in `model_post_init` (or on first mutation write the *entire* history), and emit the create event only after the first row is durably appended.

### C6. No concurrency control — duplicate version numbers corrupt aggregate history

`__setattr__` computes `data["version"] = self.version + 1` from a stale in-memory copy. Two concurrent writers that both `hydrate` vN will both append `version = N+1` (with different `version_id`s). There is:

- **No** `UNIQUE (id, version)` constraint on `records` (`persistence/models.py`).
- **No** optimistic-lock `WHERE version = N` on insert.
- No tie-break in `latest()` (`order_by(version.desc())`, `limit(1)` — arbitrary winner among ties) — `store.py:108-113`.

Additionally, DBOS transaction steps are re-executed after crash windows (commit happened but step output wasn't recorded). Because `version_id` is a fresh `uuid4()` per attempt, a re-execution inserts a **second row with the same version number** — the version_id PK cannot deduplicate logically identical mutations. "Immutable, version-tracked" history is thus not trustworthy under any concurrency or crash recovery.

**Fix:** `UNIQUE(id, version)` + retry/conflict handling, deterministic version_id derived from (id, version) for idempotent inserts, and a documented concurrency story (e.g., serialize per-aggregate mutations via the per-class queue, which the design already half-implies with `concurrency=1`).

---

## 3. HIGH

### H1. Properties bag mutations are silently lost unless re-assigned

`PropertiesBase.add/remove` (`core/properties.py:20-27`) mutate the bag in place. The record only persists a new version when `__setattr__` is triggered — i.e., the user must do the demo's contortion:

```python
props = s.properties
props.add(status="published")
s.properties = props          # the only reason the change persists
```

`props.add(...)` alone is invisible to the store; the data is silently dropped on the next hydrate. The README markets `add / remove / list` helpers as a feature but never tells users they must re-assign to persist. Footgun with silent data loss.

**Fix:** make `PropertiesBase` a `Record`-aware object (or have `add/remove` trigger the owner's version bump / re-persist).

### H2. Reads inside a DBOS transaction are non-transactional

`hydrate`/`latest`/`stream` open **their own** `Session(self.engine)` (`store.py:25-26, 104-105, 108-113`) rather than using the ambient DBOS transaction session. Inside `@Eventic.transaction()`:

- Writes go to the DBOS transaction session (uncommitted).
- Subsequent reads in the same transaction go to a *different connection* and cannot see those uncommitted writes (read-your-own-writes broken).
- A `hydrate` → `mutate` → `hydrate` sequence inside one transaction returns stale data for the second hydrate.

Combined with C6, this also means the demo's queue steps (each `hydrate`+`append` in its own transaction) work only by accident of serialization (`concurrency=1`) and per-step commits.

### H3. `Eventic.init`/`create_app` singleton silently ignores re-initialization; returned app can be unwired

`runtime.py:42-56`:

```python
if cls._singleton is None:
    cls._singleton = cls(config=cfg, fastapi=fastapi)
    cls._engine = create_engine(...)
    init_eventic(cls._engine)
return cls._singleton
```

- A second `create_app("other", db_url=…)` returns a **brand-new FastAPI app** that was never passed to DBOS (`DBOS.__new__` returns the existing global instance and *does not* re-run `__init__`/middleware setup), so the second app never gets DBOS middleware/startup launch wiring. Silent misconfiguration.
- There is no `destroy()` support (DBOS provides `DBOS.destroy()`; Eventic doesn't expose it), so tests and multi-app processes cannot be re-initialized cleanly; the global DBOS registry/instance leaks across "apps".
- `Eventic` subclasses `DBOS` but DBOS's singleton is keyed on the *global instance*, not the subclass — `Eventic` and `DBOS` are interchangeable singletons, which the class hierarchy doesn't make obvious.

### H4. No `class_type` filtering on any read path — cross-class contamination

`class_type` is written on every row (`store.py:76`) but **never read**:

- `hydrate(rec_id)` doesn't check it: `Story.hydrate(todo_id)` returns a `Story` constructed from `Todo` data. Wrong-typed objects, silently.
- `find_by_properties`/`where` return ids across all classes: `Story.where(status=…)` can match a `Todo` row.

With single-table storage this is a schema-level integrity hole. Every query should constrain `class_type` to the requesting class (and its registered subclasses).

### H5. `Eventic`'s SQLAlchemy engine and DBOS's engine diverge (driver + lifecycle)

`runtime.py:53` creates a psycopg2 engine (`create_engine("postgresql://…")`); DBOS 1.5.0 uses psycopg3. Two independent connection pools, two drivers, and `records` table creation via `Base.metadata.create_all(engine)` happens at `init` time — while DBOS creates/migrates its own system tables only at `launch()`. The `records` table has **no Alembic migration** (see M5), so any deployment that runs `alembic upgrade head` (as `dbos-config.yaml` does) never creates it — it only exists because `init_eventic` runs `create_all`. Production story is incoherent.

### H6. Event handlers: name-keyed registry collisions, exception propagation, ordering, timing

`events.py`:

- **Registration keyed by class *name*** (`self._handlers[event_type][class_name]`): two `Story` classes in different modules cross-fire handlers.
- **No isolation:** a raising handler propagates into `model_post_init`/`__setattr__`, turning a failed handler into a failed mutation.
- **Before persistence:** create events fire at construction (C5); update events fire inside the transaction before commit (H2) — handlers cannot rely on the store state.
- **Non-deterministic order:** handlers are stored in a `set` and iterated in arbitrary order.
- `_class_map` (events.py:23, 32-33) is built but never used — dead code.

### H7. In-memory object and persisted row can diverge (validation/coercion asymmetry)

`__setattr__` re-validates the *entire model* (`new_obj = self.__class__(**data)`), then reflects the **raw assigned value** locally via `object.__setattr__` (`record.py:103-107`). Any coercion pydantic performs on `new_obj` (e.g., `int`→`bool`, `float`→`int`, `str`→`datetime`, `uuid` normalization) is persisted, while the live object keeps the raw value — **local state ≠ DB state**. Conversely, an invalid assignment raises a `ValidationError` mid-mutation (after `model_dump`) that the caller could never have produced by setting a single attribute on a validated frozen model; and because the whole object is re-validated, a stale bad value anywhere in the object blocks *all* subsequent writes with a confusing error. **[verified]**

### H8. `hydrate(id, version)` semantics are surprising and under-specified

`record.py:124-143`: `hydrate(rec_id, 5)` returns the *latest row ≤ 5*, not "version 5" — the docstring says "≤ v{version}", but the error path (`raise KeyError(f"{cls.__name__} {rec_id} ≤ v{version} not found")`) and typical caller expectations (exact version) conflict. Missing rows in the middle of history silently produce an older object. Combined with H4/C6, this query is fragile. This is more of a contract ambiguity than a crash, hence "high" for API design.

---

## 4. MEDIUM

### M1. `pip install "eventic[pg]"` (README headline) fails — no extras declared

`pyproject.toml` has **no** `[project.optional-dependencies]` (verified). `pip install eventic[pg]` warns "eventic does not provide the extra 'pg'". Meanwhile `psycopg2-binary` is a *required* dependency, and `fastapi`/`sqlalchemy` are undeclared — they only arrive transitively via `dbos` (`fastapi[standard]`) and `alembic` (→ `sqlalchemy`). The dependency contract is fragile and undocumented.

### M2. `python -m eventic.examples.demo` fails; `examples` installs as a top-level package

There is no `eventic/examples` subpackage — `src/examples` is a **separate top-level package** (verified). README's `python -m eventic.examples.demo` → `ModuleNotFoundError`. And the wheel ships `examples` at top level (hatch `packages = ["src/eventic", "src/examples"]`), which **pollutes the global namespace** in site-packages and collides with any other project's `examples` package.

### M3. `dbos-config.yaml` is broken

- `runtimeConfig.start: "python3 src/eventic/app/main.py"` — **no such path** (file is `src/eventic/main.py`; verified `src/eventic/app` doesn't exist).
- `database.migrate: alembic upgrade head` — `alembic.ini` points to `%(here)s/migrations` which **does not exist** (verified). Every `dbos` run under this config fails at startup.

### M4. `where`/`find_by_properties` filter values must be JSON-serializable, and the docs lie about it

Filter values are bound into a JSONB `contains` (`store.py:124-126`). A `UUID` or `datetime` value (natural for `props.add(user_id=uuid)`) fails to bind in psycopg2/JSONB. Users must remember to stringify. No normalization, no error message explaining what to do. Also the demo's `Story._store.find_by_properties(...)` returns ids (see C4).

### M5. No Alembic migrations for `records`; `alembic.ini` points at a nonexistent directory

`src/eventic/persistence/models.py` defines the table, but there is no `migrations/` tree, no `env.py`, nothing versioned. README's "use Alembic for further schema" is aspirational; schema drift is unmanaged and `alembic upgrade head` fails out of the box.

### M6. Version metadata is attacker-controllable through the webhook

`main.py` webhook does `Story(**body)` with `extra="allow"` — an unvalidated request body can set `version`, `version_id`, `id`, and `properties.record_type` (e.g., spoof `record_type: "Admin"`), suppressing `emit_create` (`version != 0` or non-None `id`). Even though the current webhook never persists, this is a template for how injection happens once persistence is fixed. Also `datetime.utcnow()` (`main.py:58`) is deprecated in 3.12+.

### M7. Double Queue declaration warnings at import time

The metaclass creates `cls.queue = Queue(...)` (record.py:44) **and** `evented` creates another `Queue(queue_name, concurrency=1)` per wrapped method (dispatcher.py:18). Each duplicate logs `Queue queue_story has already been declared` (verified at import of the demo `Story`). Queues are also constructed at import time, before `Eventic.init()` — the registry is mutated by merely importing a module that defines a Record subclass.

### M8. `init_eventic` isn't exported; error messages reference a function users can't import

`_ensure_store` says "Call init_eventic(engine)…" (record.py:147-149), but `src/eventic/__init__.py` has `# from .bootstrap import init_eventic` **commented out**. Users must know to `from eventic.bootstrap import init_eventic`. The public entry point is actually `Eventic.init`, which the error message doesn't mention.

### M9. `Record` private-attribute handling is a misleading dead branch

`record.py:91-93` routes underscore-prefixed writes to `super().__setattr__` — which works for privates on a frozen pydantic model (verified), but the branch is unconditional: setting `s._cache = x` bypasses the entire versioning machinery silently, while setting `s.version = 5` (a *public* field) is silently overwritten by `data["version"] = self.version + 1`. Two different silent behaviors for "special" attribute names, neither documented.

### M10. `Eventic.queue()` dead whitelist + `concurrency=None` passthrough

`runtime.py:60-64` contains a commented-out queue allowlist and passes `concurrency=None` straight to DBOS. If `None` is not a meaningful "default" for DBOS `Queue`, callers get default concurrency silently; the API pretends to offer control it doesn't document.

### M11. Duplicate/dead code in the store

`latest_sync`/`stream_sync` (store.py:28-48) duplicate `latest`/`stream`, are unused, and `stream_sync` yields `r.RecordRow` (an entity accessor) while the module's own `stream` was redefined twice (store.py:94 and 139 — the second definition shadows the first inside a comment block, leaving confusing dead code). `Record.hydrate` references `latest_sync?` in a comment (record.py:129).

### M12. No tests, no CI, and the tests package isn't shipped

`src/tests/__init__.py` is empty (verified). None of C1–C6 could have survived even a smoke test. `src/tests` is not in the wheel `packages` list, so even shipped tests would be absent.

---

## 5. LOW / code quality

1. **`ClassVar` is used but never imported** in `record.py:58` (`_store: ClassVar[...]`) — it only works because `from __future__ import annotations` stringifies the annotation and pydantic tolerates the unresolvable forward ref. Fragile; imports should match usage. **[verified working today]**
2. **`model_dump(mode="python")` vs `mode="json"` asymmetry** (`record.py:94` vs `store.py:79`) — the versioning pipeline mixes python-object dumps and JSON dumps; custom types with `arbitrary_types_allowed` behave differently in each mode.
3. **`_snake` edge cases** (`record.py:28-29`): class names like `My2FA` → `my2_fa` (fine), but names with non-ASCII or unusual casing produce queue names that may collide or violate DBOS naming expectations.
4. **`Record.__setattr__` churn**: assigning the same value still writes a new version row (no change detection) — version tables will bloat.
5. **`properties` column duplicates `data.properties`** (`store.py:77` vs `store.py:79`) — redundant storage of the same JSONB blob.
6. **`RecordRow.properties` is nullable** yet always populated; nullable column invites NULL-vs-empty inconsistencies in `contains` queries (NULL never matches).
7. **`demo.py` calls `Eventic.init` after `create_app`** (`demo.py:141`), which is a guaranteed no-op (H3) — a confusing pattern presented as the canonical example; and `main.py`/`demo.py` hardcode `@postgres:5432` docker hostnames and require `POSTGRES_*` env vars, ignoring `DBOS_DATABASE_URL` that the README tells users to export.
8. **`Eventic.launch()` + ASGI auto-launch double path**: `create_app` wires auto-launch on ASGI startup, and `demo.py` also calls `Eventic.launch()` manually — two ways to start the same executor; DBOS guards it with a warning, but the API offers no clarity about which is authoritative.
9. **`eventic.main:start` console script** runs a hardcoded docker-compose webhook app; `eventic-example` script name vs README's `python -m …` instructions are two divergent entry points.
10. **`main.py` returns `(content, 400)` tuple** — legal in FastAPI but inconsistent with the rest of the handler's plain-dict returns; also swallows exceptions into HTTP 200/400 without logging the stack.
11. **`dbos-config.yaml` schema drift**: keys (`hostname/port/username/password/app_db_name`) do not match the DBOS 1.5 config schema (`database_url` etc.), so the file would be rejected/misread by `dbos` CLI.
12. **README claims "run now and later" as a feature** — this is C2's double-execution; the documentation enshrines a bug as a selling point.

---

## 6. What's genuinely good

Fairness requires noting the strengths:

- **Clean separation of concerns:** `core/` is pure-Pydantic (no DBOS/Postgres imports); `persistence/` and `queues/` are isolated; `runtime.py` centralizes the DBOS façade. The architecture is easy to reason about and test in isolation.
- **Single-table versioning design** (PK `version_id`, stable aggregate `id`, JSONB `data`) is a sound event-sourcing-lite shape; the `DISTINCT ON (id) … ORDER BY version DESC` latest-properties query (`store.py:116-124`) is correct Postgres.
- **`hydrate(id, version)` streaming with early termination** is a reasonable append-only history read.
- `properties` JSONB containment search via `where` is a nice ergonomic (once C4/H4 are fixed).
- Docstrings and comments are unusually thoughtful (e.g., the append docstring honestly explains the DBOS-context design intent — it's just that the standalone branch was never finished).

---

## 7. Prioritized remediation roadmap

| Priority | Finding | One-line fix |
|---|---|---|
| P0 | C1 | Restore standalone-session fallback in `RecordStore.append`; add out-of-context mutation test |
| P0 | C2/C3 | Replace blanket metaclass wrapping with explicit opt-in decorator; never wrap staticmethods/internals |
| P0 | C5 | Persist v0 at construction; emit create event only after durable append |
| P1 | C6 | `UNIQUE(id, version)`; deterministic version_id; conflict handling |
| P1 | C4/H4 | `where` hydrates ids; all reads filtered by `class_type` |
| P1 | H1 | Make `PropertiesBase` mutations persist via owner callback |
| P2 | H2/H5/H6/H7 | Use DBOS ambient session for reads when inside a transaction; single engine story; key handlers by class object; document/validate coercion semantics |
| P2 | H3 | Allow explicit re-init with `DBOS.destroy()`; validate re-init warnings |
| P3 | M1–M5, M7, M8 | Fix pyproject deps/extras; move examples under `eventic/`; fix dbos-config + alembic tree; export `init_eventic`; dedupe queue creation |
| P3 | M12 | Add smoke tests for every P0 item before any further feature work |

---

## Appendix A — Verification probes (all run against the repo's `.venv`)

| Probe | What it proved |
|---|---|
| `probe1.py` | `ClassVar` referenced without import in a string annotation is tolerated (L1 is latent, not fatal) |
| `probe2.py` | `callable(classmethod)` = **False**, `callable(staticmethod)` = **True** in 3.13 → C3 |
| `probe4.py` | frozen pydantic model rejects public attribute writes (`frozen_instance`), allows private ones → M9 context |
| `probe5.py` | `evented`-wrapped method: synchronous body runs, then `DBOSException("No DBOS was created yet")` → C2 |
| `probe6.py` | `import eventic` + `Story()` works; `Queue queue_story has already been declared` warning at import (M7); `Story.ping(5)` on a `@staticmethod` raises `DBOSException` (C3) |
| `probe7.py` | `new_obj = P(**data)` re-validates whole model; invalid typed assignment raises `ValidationError` mid-mutation (H7) |
| DBOS source (`_dbos.py:1079`, `_context.py:271`, `_core.py:start_workflow`, `_queue.py:29-88`) | `DBOS.sql_session` asserts transaction context (C1); `start_workflow` rejects unregistered funcs (C2); `Queue.enqueue` requires a launched DBOS (C2) |
| Filesystem checks | `migrations/` absent (M5), `src/eventic/app/` absent (M3), `src/tests/` empty (M12), `optional-dependencies` absent (M1), `eventic.examples` not importable (M2) |

---

*Reviewer note: all line numbers refer to the repository as checked out at `a65ac96`; the DBOS-side references are to the installed `dbos==1.5.0` under `.venv/`. Severity judgments assume this is a library meant for public consumption ("MIT-licensed", PyPI badges in the README).*
