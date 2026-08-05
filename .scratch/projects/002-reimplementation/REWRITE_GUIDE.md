# Eventic — Complete Rewrite Guide

**Goal:** rebuild eventic from scratch around `CONCEPT.md` (the invariants I1–I7 and
the write/read pipeline) and `PLUGINS.md` (the closed five-seam plugin framework).
This is **not** the incremental thin-rewrite in `REIMPLEMENTATION_PLAN.md` — it
replaces the whole implementation. It reuses that plan's data-safety work (the C6
backfill, the deterministic-version_id insight) but restructures the code into the
plugin architecture.

**Target stack:** Python ≥3.13 · Pydantic 2.11 · SQLAlchemy 2.0.51 · **DBOS optional**
(`eventic[dbos]` only). **Convention:** one commit per step; every step has an
**exit gate** (tests + acceptance) and a **rollback**. Run
`.venv/bin/python -m pytest src/tests -q` at each gate.

**Sub-branch:** `git checkout -b rewrite/plugin-core` off `reimagine/first-principles`
so the review/plan branch stays intact as the reference.

> **Guardrail (PLUGINS §8.5) honored throughout:** the plugin *framework* is only
> extracted (Phase 2) once a *second* real plugin needs it. Phase 1 builds the core
> with the five defaults behind clean internal interfaces; Phases 3–4 add the two
> real plugins (DBOS delivery, diff codec) that justify the framework's existence.
> If only one ever ships, Phase 2 collapses back to two if-branches cheaply.

---

## Target module layout (the destination)

```
src/eventic/
  __init__.py          # Record, connect, on_commit, use, errors, plugin base exports
  errors.py            # StaleVersionError, PluginConflictError, MissingCapability, NotConnected
  connect.py           # connect(url) + the engine registry
  models.py            # the `records` table (used by the default persistence plugin)
  record.py            # Record: pure construct + save/update/edit/commit + get/history/where
  pipeline.py          # write/read orchestration; dispatches to the class's seam providers
  events.py            # Event, on_commit registry (sync default lives in delivery)
  plugins/
    __init__.py        # Plugin base, Seam enum, registry, the class assembler
    persistence.py     # PersistencePlugin ABC + SingleTableJSONB (default)
    codec.py           # CodecPlugin ABC + FullSnapshot (default) + DiffStorage
    identity.py        # IdentityPlugin ABC + Uuid5Deterministic (default)
    delivery.py        # DeliveryBackend ABC + SyncDelivery (default, mode="sync")
    interceptor.py     # Interceptor ABC + ordering helpers
  dbos/                # OPTIONAL — installed via eventic[dbos]
    __init__.py        # DurableEvents (delivery plugin), durable, queue, create_app
  examples/
    demo.py            # rewritten on the new API
    webhook.py         # was main.py
migrations/            # Alembic: fresh initial + fold-properties + (kept) C6 backfill
src/tests/
  core/                # DBOS-free suite (fast)
  plugins/             # codec/persistence/interceptor tests
  dbos/                # runs under the [dbos] extra
```

---

## Phase 0 — Scaffolding & safety net

### Step 0 — Baseline, skeleton, errors

1. Confirm the current suite is green (28 passed) and record it; this old suite is the
   **behavioral reference** until Phase 5 deletes the old code.
2. `git checkout -b rewrite/plugin-core`.
3. Create the empty package skeleton above (empty modules with docstrings) **without
   removing** any existing module — old and new coexist until the Phase-5 swap. Keep
   `src/tests/` old tests running against old code; put new tests under
   `src/tests/core|plugins|dbos`.
4. `errors.py`:
   ```python
   class EventicError(Exception): ...
   class NotConnected(EventicError): ...
   class StaleVersionError(EventicError):           # I5
       def __init__(self, id, version):
           super().__init__(f"aggregate {id} already has a different version {version}")
           self.id, self.version = id, version
   class PluginConflictError(EventicError): ...      # two providers, one exclusive seam
   class MissingCapability(EventicError): ...        # requires token unmet
   ```

**Exit gate:** package imports (empty), old suite still 28 green.
**Rollback:** delete the new skeleton dirs; nothing referenced them.

---

## Phase 1 — The invariant core (no plugins exposed, no DBOS)

Build a working DBOS-free library with the five defaults wired directly. This phase
alone is a shippable `0.2.0-alpha` that already fixes R-C1/R-E1/R-P3.

### Step 1 — `connect()`, the engine registry, and `models.py`

1. `models.py` — the single append-only table (unchanged shape from today minus the
   redundant `properties` column; `meta` now lives inside `data`):
   ```python
   class RecordRow(Base):
       __tablename__ = "records"
       __table_args__ = (UniqueConstraint("id", "version", name="uq_records_id_version"),)
       version_id = Column(Uuid(as_uuid=True), primary_key=True)
       id         = Column(Uuid(as_uuid=True), nullable=False, index=True)
       version    = Column(Integer, nullable=False)
       class_type = Column(String, nullable=False)
       created_ts = Column(DateTime(timezone=True), nullable=False, default=now_utc)
       data       = Column(JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False)
   __table_args__ += (Index("ix_records_id_ver", "id", "version"),)
   ```
2. `connect.py` — a process engine registry (replaces `Eventic.init`/`init_eventic`,
   no DBOS):
   ```python
   _ENGINE: Engine | None = None
   def connect(url: str, *, create_tables: bool = True) -> None:
       global _ENGINE
       _ENGINE = create_engine(_normalize_pg(url), future=True, pool_pre_ping=True)
       if create_tables: Base.metadata.create_all(_ENGINE)   # dev convenience; alembic in prod
   def engine() -> Engine:
       if _ENGINE is None: raise NotConnected("call eventic.connect(url) first")
       return _ENGINE
   ```

**Tests (`tests/core/test_connect.py`):** `connect` idempotent-ish (re-connect swaps
engine); `engine()` before connect raises `NotConnected`.
**Exit gate:** new tests pass; old suite untouched.
**Rollback:** delete both modules.

### Step 2 — `Record`: pure construction & managed fields (I3, I4)

```python
class Record(BaseModel):
    model_config = {"extra": "allow", "arbitrary_types_allowed": True}   # NOT frozen
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    version: int = 0
    version_id: uuid.UUID | None = None      # filled by the identity provider at commit
    created_ts: datetime | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, _):            # PURE: assign identity of v0 only, NO I/O (I3)
        if self.version_id is None:
            object.__setattr__(self, "version_id", _uuid5(self.id, self.version))
```

No metaclass, no auto-persist, no event emission at construction. `_uuid5` is the
deterministic identity helper (I4), applied to v0 too — closing R-C2.

**Tests (`tests/core/test_record_pure.py`):** `Record(...)` writes **no** rows (assert
the table is empty after construction — the I3/R-E1 regression); v0 `version_id` is
deterministic (`uuid5`); two constructions with the same `id`+`version` produce the
same `version_id`.
**Exit gate:** construction is provably I/O-free.
**Rollback:** delete `record.py` additions.

### Step 3 — Persistence + codec + identity + explicit writes (I1, I2, I4, I5)

Wire the three exclusive defaults directly (the seam ABCs come in Phase 2). Implement
`save/update/commit/get/history/where` in `record.py`, delegating to `pipeline.py`.

`plugins/persistence.py` — `SingleTableJSONB.append` with the **loud-conflict** logic:
```python
def append(self, row: dict) -> None:
    eng = engine()
    with Session(eng, future=True) as s:
        try:
            s.execute(insert(RecordRow).values(**row))
            s.commit()
        except IntegrityError:                       # (id, version) collision
            s.rollback()
            existing = s.execute(
                select(RecordRow.version_id)
                .where(RecordRow.id == row["id"], RecordRow.version == row["version"])
            ).scalar_one()
            if existing == row["version_id"]:
                return                                # byte-identical replay -> idempotent no-op (I5)
            raise StaleVersionError(row["id"], row["version"])   # different writer -> LOUD (I5, R-C1)
```
`latest/at/stream/query` as `select`s scoped by `class_type` (H4). `FullSnapshot.encode`
= `new.model_dump(mode="json")`; `decode(rows)` = `rows[-1].data`. `Uuid5Deterministic`
= `_uuid5(id, version)`.

`record.py` write API:
```python
def save(self) -> Self:                    # persist v0 explicitly (I2)
    _pipeline.commit_version(self, prev=None, kind="create"); return self
def update(self, **changes) -> Self:       # returns the NEW version; original untouched
    data = self.model_dump(mode="python") | changes
    data["version"] = self.version + 1
    data["version_id"] = _uuid5(self.id, data["version"])
    new = type(self)(**data)
    _pipeline.commit_version(new, prev=self, kind="update"); return new
```
`get/history/where` read via the pipeline's read path (decode + class_type scope).

**Tests (`tests/core/test_persistence.py`):** save→get roundtrip; update returns new
version and leaves original object unchanged; `history` ordered; `where` by field and
by `meta`; **`StaleVersionError` on two different writers at the same version**
(the probe_02 scenario now raises); **idempotent replay** (same version_id twice → one
row). Port probe_02 as a regression test asserting the *loud* behavior.
**Exit gate:** the R-C1 silent-loss scenario now raises; all read/write core tests green.
**Rollback:** revert `record.py`+`persistence.py`; Step 2 stands alone.

### Step 4 — `edit()` batch writes (R-P1)

```python
@contextmanager
def edit(self):
    draft = self.model_dump(mode="python")
    box = _EditProxy(draft)          # collects field sets in memory only
    yield box
    if box.dirty:                    # ONE new version for all changes (validate+append once)
        self.update(**box.changes)
```
**Tests:** `with r.edit() as e: e.a=1; e.b=2` writes exactly one version (assert history
length); empty edit writes nothing (no-op guard, no throwaway object built).
**Exit gate:** batching writes one version; write-amplification path fixed.

### Step 5 — Events core: post-commit sync delivery (I7)

`events.py`:
```python
@dataclass(frozen=True)
class Event: kind: str; record: "Record"; delta: dict | None = None
_HANDLERS: dict[type, list[tuple[str, Callable, str]]] = defaultdict(list)  # cls -> (kind, fn, mode)
def on_commit(*classes, kind="*", mode="sync"):
    def deco(fn):
        for c in classes: _HANDLERS[c].append((kind, fn, mode))
        return fn
    return deco
```
`SyncDelivery` (in `delivery.py`, the default `mode="sync"` backend) runs matching
handlers **after** `append` returns and commits (I7), isolated + in MRO order:
```python
def deliver(self, event):
    for c in type(event.record).__mro__:
        for kind, fn, mode in _HANDLERS.get(c, []):
            if mode != "sync" or (kind not in ("*", event.kind)): continue
            try: fn(event.record)
            except Exception: logger.exception("handler %s failed", fn.__name__)
```
`pipeline.commit_version` calls `delivery.deliver(Event(kind, record, delta))` as its
final step — strictly post-persist (I7 fixes R-C4). `save` emits `create`, `update`
emits `update` with the field-level `delta`.

**Tests (`tests/core/test_events.py`):** handler fires post-commit and can `get()` the
record (H6 timing); failing handler isolated; registration order; keyed by class object
(a `Note` handler never fires for `Doc`); update handler receives the delta.
**Exit gate:** **Phase 1 complete** — a DBOS-free, plugin-less-facing library that
upholds I1–I7. Time the core suite: should be ~1s, not ~35s (R-P3). Tag `0.2.0-alpha`.
**Rollback:** the phases are additive; revert to Step 4.

---

## Phase 2 — Extract the plugin framework

Now that Phase 3/4 will add real alternative providers, formalize the seams.

### Step 6 — Plugin base, seams, the class assembler (PLUGINS §2,§3,§5)

`plugins/__init__.py`:
```python
class Seam(str, Enum):
    PERSISTENCE="persistence"; CODEC="codec"; IDENTITY="identity"
    DELIVERY="delivery"; INTERCEPTOR="interceptor"
EXCLUSIVE = {Seam.PERSISTENCE, Seam.CODEC, Seam.IDENTITY}

class Plugin:
    seam: Seam
    provides: set[str] = set()      # capability tokens, e.g. {"persistence:json"}
    requires: set[str] = set()
    priority: int = 0

def assemble(cls, plugin_classes: list[type[Plugin]]):
    chosen: dict[Seam, list] = defaultdict(list)
    provided: set[str] = set()
    for p in plugin_classes:
        chosen[p.seam].append(p); provided |= p.provides
    for seam in EXCLUSIVE:
        if len(chosen[seam]) > 1:
            raise PluginConflictError(f"{cls.__name__}: multiple {seam.value} providers: {chosen[seam]}")
    for p in plugin_classes:
        missing = p.requires - provided
        if missing: raise MissingCapability(f"{cls.__name__}: {p.__name__} needs {missing}")
    cls.__eventic_plugins__ = plugin_classes
    cls._persistence = (chosen[Seam.PERSISTENCE] or [SingleTableJSONB])[0]()
    cls._codec       = (chosen[Seam.CODEC] or [FullSnapshot])[0]()
    cls._identity    = (chosen[Seam.IDENTITY] or [Uuid5Deterministic])[0]()
    cls._interceptors= sorted(chosen[Seam.INTERCEPTOR], key=lambda p: p.priority)
    # delivery providers register their mode into the global mode->backend registry
```
`Record.__init_subclass__` calls `assemble(cls, [b for b in bases if issubclass(b, Plugin)])`
plus any global defaults set via `use(...)`. **All validation happens here — at class
definition, never at import or first call** (kills the same-name-class / import-time
findings).

Convert Phase-1 defaults into `Plugin` subclasses (behavior unchanged): `SingleTableJSONB(Plugin, seam=PERSISTENCE, provides={"persistence:json","persistence:transactional"})`,
`FullSnapshot(codec)`, `Uuid5Deterministic(identity)`, `SyncDelivery(delivery, mode="sync")`.
`pipeline.py` now dispatches through `cls._persistence/_codec/_identity/_interceptors`
and the delivery registry instead of hardcoded calls.

**Tests (`tests/plugins/test_assembly.py`):** two codecs on one class →
`PluginConflictError` at definition; a plugin with an unmet `requires` →
`MissingCapability`; `Doc.__eventic_plugins__` introspection; defaults resolve when no
plugin is attached (every Phase-1 test still green).
**Exit gate:** all Phase-1 tests green **through the seam dispatch**; assembly tests pass.
**Rollback:** the defaults still work if you revert to the direct calls; keep Step 5 tag.

---

## Phase 3 — The DBOS delivery plugin (`eventic[dbos]`)

### Step 7 — `DurableEvents`, `@durable`, `queue`, `create_app` (delivery seam)

`src/eventic/dbos/__init__.py` (imported only under the extra):
```python
class DurableEvents(Plugin):
    seam = Seam.DELIVERY
    requires = {"persistence:transactional"}          # outbox must be atomic with the append
    mode = "durable"
    def deliver(self, event):
        # enqueue (id, kind) — NOT the pickled record — onto the class's DBOS queue,
        # inside the same DBOS transaction that wrote the version (transactional outbox).
        _queue_for(type(event.record)).enqueue(_run_handlers, str(event.record.id), event.kind)
```
```python
def durable(fn):                       # explicit DBOS step registration (replaces @evented magic)
    return DBOS.step()(fn)
def queue(name, *, concurrency=None) -> Queue: ...
def create_app(name, *, db_url, **fk) -> FastAPI: ...   # FastAPI + DBOS wiring, no Eventic(DBOS) subclass
```
Handlers registered `@on_commit(Order, mode="durable")` run later, get an **id**,
re-hydrate, and must be idempotent (the async contract, documented). `pyproject.toml`:
move `dbos`, `fastapi`, `uvicorn` out of core deps into `[project.optional-dependencies].dbos`.

**Tests (`tests/dbos/`, gated on the extra; DBOS supports SQLite so no Postgres):**
`test_durable_handler_runs_after_commit_via_queue`; `test_durable_handler_gets_id_not_record`
(assert the enqueued arg is a str id, not a pickled Record — closes R-S1);
`test_core_import_is_dbos_free` (`import eventic; assert 'dbos' not in sys.modules`).
**Exit gate:** core stays DBOS-free; durable delivery works under the extra; the
same-name-class limitation is gone (queues created explicitly, not at import per class).
**Rollback:** delete `eventic/dbos/`; the `durable` mode simply isn't registered.

---

## Phase 4 — The diff-storage codec plugin (justifies the framework)

### Step 8 — `DiffStorage` (codec seam): forward deltas + snapshot-every-K

```python
class DiffStorage(Plugin):
    seam = Seam.CODEC
    requires = {"persistence:json"}         # incompatible with TypedTable -> caught at definition
    K = 20                                   # snapshot interval (tunable per subclass)
    def encode(self, prev, new):
        if prev is None or new.version % self.K == 0:
            return {"kind": "snapshot", "state": new.model_dump(mode="json")}
        return {"kind": "delta", "patch": _field_diff(prev.model_dump(mode="json"),
                                                       new.model_dump(mode="json"))}
    def decode(self, rows):                  # rows oldest->newest up to target version
        base = next(r for r in reversed(rows) if r.data["kind"] == "snapshot")
        state = dict(base.data["state"])
        for r in rows[rows.index(base)+1:]:
            state = _apply_patch(state, r.data["patch"])
        return state
```
`persistence.stream` already returns the rows the codec needs; `get(id)` fetches from
the nearest snapshot forward (bounded by K). `where()` scopes to snapshot rows (or a
materialized head) so JSONB queries keep working — documented (§CONCEPT read path).

**Tests (`tests/plugins/test_diff_codec.py`):** encode→decode roundtrip across a chain
longer than K (snapshot + deltas); reconstruction at an arbitrary version matches the
full-snapshot library byte-for-byte; a large-body aggregate edited N times stores
≪ N×body bytes (assert the size win); `DiffStorage` + `TypedTable` on one class →
`MissingCapability` at definition (proves the guardrail). This is the **second real
plugin** that retroactively justifies Phase 2.
**Exit gate:** diff and snapshot codecs produce identical hydrated objects; framework
validated by two independent plugins.
**Rollback:** delete `DiffStorage`; `FullSnapshot` remains the default.

---

## Phase 5 — Public surface, examples, data migration, docs

### Step 9 — Public API + rewritten examples

`__init__.py`: `Record, connect, on_commit, use, DiffStorage, StaleVersionError, Plugin`
(+ `from eventic.dbos import ...` only if installed). Rewrite `examples/demo.py` and
`examples/webhook.py` (was `main.py`) on the new API: explicit `.save()`, `mode="durable"`
handlers, `queue().enqueue(fn, id)`. Optional: implement `hair_trigger=True` as an
explicitly-invariant-relaxing subclass flag (documented as "scripts only; violates I2")
— off by default; include only if you want the scripting affordance.

**Tests:** `examples/webhook.py` end-to-end (post → persisted v0 → durable reindex);
metadata-injection still rejected (M6).
**Exit gate:** `python -m eventic.examples.demo` runs; webhook test green.

### Step 10 — Data migration from the 0.1.x `records` table

Alembic revision `fold_properties_into_data`:
```sql
UPDATE records SET data = jsonb_set(data, '{meta}', COALESCE(properties, '{}'::jsonb)); -- PG
ALTER TABLE records DROP COLUMN properties;                                             -- PG
```
SQLite branch: table-rebuild (create new without `properties`, copy with
`json_set(data,'$.meta',properties)`, drop, rename). Keep the shipped `a1b2c3d4e5f6`
C6 backfill in the chain (dedupe + unique constraint) so pre-0.2 duplicates are handled
first. Old rows keep their random v0 `version_id` (I4 only guarantees determinism for
rows written by 0.2+ — documented; the unique constraint still protects them).
`downgrade` re-adds `properties` from `data.meta`.

**Exit gate:** `alembic upgrade head && alembic downgrade base` round-trips on scratch
SQLite **and** Postgres; a table written by the OLD library hydrates under the NEW one.
**Rollback:** `downgrade` is reversible; review the `UPDATE` on staging first.

### Step 11 — README + MIGRATION.md + pyproject

- README to `CONCEPT.md §9` positioning; document the pipeline, invariants, `on_commit`
  modes, plugins, and the **loud** `StaleVersionError` concurrency story.
- `MIGRATION.md` (0.1.x → 0.2): `Eventic.init`→`connect`; `s.x = y`→`.update`/`.edit`;
  `properties`→`meta`; `on.create/update`→`on_commit`; `@evented`→`@on_commit(mode="durable")`
  or `eventic.dbos`; the DB migration order.
- `pyproject.toml`: core deps = `pydantic, sqlalchemy, python-dotenv, alembic`;
  `[dbos]` extra = `dbos, fastapi, uvicorn`; drop the `Eventic(DBOS)` console script.

**Exit gate:** `pip install -e .` (no dbos) imports and runs the core suite;
`pip install -e '.[dbos]'` runs the dbos suite.

---

## Phase 6 — Delete the old implementation & final validation

### Step 12 — Remove the old modules (the swap)

Delete `core/record.py` (old), `core/properties.py`, `queues/dispatcher.py`,
`runtime.py`, `bootstrap.py`, `main.py`, and the old `src/tests/*` that tested the
removed mechanisms (their behavioral intent now lives in `tests/core|plugins|dbos`).
Grep to ensure nothing imports the deleted names.

**Exit gate:** `grep -rE "RecordMeta|evented|Eventic\(|PropertiesBase|_owner" src/eventic`
returns nothing; full new suite green.
**Rollback:** this is the point of no return — keep the `0.2.0-alpha` (Phase 1) and the
post-Step-8 states as shippable fallbacks.

### Step 13 — Final validation matrix

| check | command |
|---|---|
| Core import is DBOS-free | `python -c "import sys, eventic; assert 'dbos' not in sys.modules"` |
| Core suite (fast) | `pytest src/tests/core -q` (target ~1s) |
| Plugin suite | `pytest src/tests/plugins -q` |
| DBOS suite | `pip install -e '.[dbos]' && pytest src/tests/dbos -q` |
| No hidden writes (I2/I3) | `test_construction_writes_nothing` green |
| Loud conflicts (I5) | `test_two_writers_raise_stale_version` green |
| Post-commit events (I7) | `test_handler_sees_committed_row` green |
| Plugin conflicts fail at definition | `test_two_codecs_conflict` green |
| Migrations | `alembic upgrade head && alembic downgrade base` on SQLite + PG |
| Warnings clean | `pytest -W error` green |

**Exit gate:** every row passes; `git log` shows one commit per Step 0–13.

---

## Finding → step map (every REIMAGINE_REVIEW finding closed)

| finding | closed by |
|---|---|
| R-C1 silent lost update | Step 3 (loud `StaleVersionError`) |
| R-C2 v0 non-idempotent identity | Step 2 (uuid5 for v0) |
| R-C3 no identity map | Step 3 (`update` returns new; no stale-alias auto-write) |
| R-C4 pre-commit events | Step 5 (post-commit `deliver`, I7) |
| R-C5 `_session` fallback | Steps 3/7 (core one-engine; DBOS ctx handled in the plugin) |
| R-S1 pickle RCE | Step 7 (durable handlers enqueue **ids**, not Records) |
| R-E1 hidden writes | Steps 2–4 (pure construct; explicit `save/update/edit`) |
| R-P1 write amplification | Step 4 (`edit` batches) + Step 8 (diff codec shrinks each write) |
| R-P2 where() scan | Step 3 (class_type scope) + Step 11 (ship the PG GIN index) |
| R-P3 DBOS tax | Steps 1/5/7 (DBOS out of the core import graph) |
| R-M1 hidden globals | Steps 1/6 (one engine registry; explicit plugin assembly) |
| R-M2 metaclass | Steps 2/6 (`__init_subclass__` assembler, no metaclass) |
| R-M3 tests bless the bug | Step 3 (probe_02 ported as a *loud* regression) |
| R-S2 extra="allow" | Step 9 (webhook DTO; document `meta` as the extension point) |
| R-X1/X2 redundancy | whole rewrite (shrink to the defensible core + compose DBOS as a plugin) |
| same-name-class crash | Steps 6/7 (no per-class import-time queue; assembly fails loud at definition) |

---

## Overall rollback strategy

- Phases 1, 3, 4 each end at a shippable state (`0.2.0-alpha` core; +dbos; +diff).
  Stop at any of them if scope needs trimming.
- The plugin framework (Phase 2) is reversible to two if-branches while only `sync`
  delivery exists; it earns its keep at Step 7 (second delivery mode) and Step 8
  (second codec).
- Step 12 is the only irreversible cut; keep the pre-Step-12 branch until the new
  suite has soaked and the data migration is validated on a production-shaped copy.

---

## Appendix — Execution notes & deviations

Dated entries recording where the plan met reality (mirrors the 001 guide's
Appendix B). Each row: date · step · deviation · reason.

- **2026-08-04 · Step 0 (carried through Phases 1–5) · D1 — the new events core
  lives at `src/eventic/eventbus.py`, not `events.py`, until Step 12.** The old
  0.1 `events.py` (`EventRegistry`, `on.create/on.update`, `emit_create`) is
  imported by the old suite (`test_record.py` uses `@on.create`), and the guide
  requires the old suite to stay green until the Phase-6 swap. Both
  implementations cannot share one module path, so the new module carries the
  working name `eventbus.py`; it is `git mv`'d to `events.py` in Step 12 when
  the old tests are deleted. The final module map is unchanged.

- **2026-08-04 · Step 3 · D2 — the replay/no-op check compares `(version_id,
  data)`, not `version_id` alone.** The guide's `append` sketch does
  `if existing == row["version_id"]: return` to distinguish a byte-identical
  replay from a different writer. But under I4 `version_id` is
  content-*independent* (`uuid5("eventic:{id}:{version}")`), so two different
  writers at the same `(id, version)` produce the *same* `version_id` — that
  check would silently classify B's write as a replay and drop it, re-opening
  R-C1. The implemented append compares `(existing.version_id == row[version_id]
  and existing.data == row[data])`; only a fully byte-identical row is the
  silent no-op, everything else raises `StaleVersionError` (I5 as written in
  CONCEPT: "only a byte-identical replay is a silent no-op").
- **2026-08-04 · Step 3 · D3 — `_uuid5` lives in `plugins/identity.py`, not
  `record.py`.** The guide sketch places it in `record.py`, but
  `plugins/identity.py` needs it and imports it from `record.py`, creating a
  cycle (`record → plugins.identity → record`). Identity is the natural leaf
  home; `record.py` imports it back. No behavior change.
- **2026-08-04 · Step 3 · D4 — the codec carries a `fetch()` read-hint.** The
  guide's Step 3 gives the pipeline `latest/at/stream/query` primitives and a
  `decode(rows)` that returns `rows[-1].data`, but nothing says how `get(id)`
  decides between "fetch one row" (cheap for `FullSnapshot`) and "fetch the
  whole stream" (needed by a diff codec). The pipeline calls
  `codec.fetch(persistence, id, class_type, version=...)` — `FullSnapshot`
  returns `[latest]`/`[at]`; `DiffStorage` (Step 8) will override it to stream
  from the nearest snapshot. Keeps `decode` pure and the pipeline codec-agnostic.
