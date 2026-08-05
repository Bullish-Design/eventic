# Eventic — The Plugin Architecture

Companion to `CONCEPT.md` (the invariants and the write/read pipeline this framework
extends). Goal: **one general extension mechanism** such that DBOS-durable delivery,
diff storage, typed-column persistence, multi-tenancy, encryption-at-rest, audit,
soft-delete, and any third-party capability are all *the same kind of thing* — a
provider attached at a named seam in the pipeline — rather than bespoke features.

The design tension is explicit and addressed in §8: a "general plugin framework" is
exactly the kind of speculative generality the review warned against. The guardrails
that keep it honest are part of the design, not an afterthought.

---

## 1. The single idea

Everything beyond the invariant core is a **plugin**: an object that provides one or
more **capabilities** at the **seams** of the write/read pipeline (`CONCEPT.md` §5–6).
The core itself is just the set of *default* plugins (`CONCEPT.md` §7). There is no
second extension mechanism — no hooks-registry-plus-mixins-plus-config sprawl. One
concept: *a provider occupies a seam.*

```
class Doc(Record, DiffStorage, DurableEvents, MultiTenant):
    body: str
#          └ codec      └ delivery      └ interceptor+schema
# Every base after Record is a plugin occupying one or more seams.
```

---

## 2. The seams (the complete, closed set)

There are exactly **five seam kinds**. Keeping this set small and closed is the
primary defense against framework sprawl — you cannot hook arbitrary internals, only
these named stages. Each seam is either **exclusive** (one provider; the default
counts) or **stacking** (0..N providers, ordered and isolated).

| seam | kind | governs (pipeline stage) | default provider | example alternatives |
|---|---|---|---|---|
| **persistence** | exclusive | `persist` / `select` — where & how rows are stored and queried | `SingleTableJSONB` | `TypedTable` (SQLModel, per-type columns) |
| **codec** | exclusive | `encode` / `decode` — how a version's state is represented in a row | `FullSnapshot` | `DiffStorage` (forward deltas + snapshot-every-K) |
| **identity** | exclusive | version_id / id derivation | `Uuid5Deterministic` | `HashOfState`, `ExternalId` |
| **delivery** | keyed registry | `deliver` — how an emitted event reaches handlers, per named *mode* | `sync` (mode always present) | `durable` (DBOS outbox), `notify` (LISTEN/NOTIFY), `arq` |
| **interceptor** | stacking | `before_commit` / `after_commit` / `after_hydrate` (+ optional `schema`) | none | `Audit`, `SoftDelete`, `Encryption`, `AccessControl`, `MultiTenant`, `Timestamps` |

**Why these five and no more:** they are the only stages in the pipeline where a
capability can meaningfully vary. Storage (where), representation (how encoded),
identity (how keyed), delivery (how events propagate), and cross-cutting behavior
(interceptors) exhaust the design space. A proposed plugin that fits none of these is
a signal it doesn't belong in eventic.

---

## 3. The plugin contract

A plugin is a class (usable as a mixin) or an object declaring **capabilities** and
optionally implementing the hook methods for the seams it occupies. It also declares
what it **provides** and **requires**, so the assembler can validate a class's plugin
set at definition time (fail-fast, never at import or first-call).

```python
class DiffStorage(Plugin):
    provides   = {"codec"}                 # occupies the exclusive codec seam
    requires   = {"persistence:json"}      # needs a JSON-capable persistence (not TypedTable)
    priority   = 0                          # ordering hint for stacking seams (ignored for exclusive)

    # codec seam methods (only the ones a codec must implement):
    def encode(self, prev: Record | None, new: Record) -> dict: ...
    def decode(self, rows: list[Row]) -> dict: ...            # replay to full state
```

The hook surface, by seam:

- **persistence:** `persist(row)`, `select_latest(id, cls)`, `select_at(id, ver, cls)`,
  `stream(id, cls)`, `query(cls, predicate)`. Declares capability tokens like
  `persistence:json` or `persistence:columns` for `requires` matching.
- **codec:** `encode(prev, new) -> stored`, `decode(rows) -> state`.
- **identity:** `version_id_for(id, version, state) -> uuid`.
- **delivery:** `register_mode() -> str`, `deliver(event, handlers)`. A handler picks a
  mode (`@on_commit(Doc, mode="durable")`); the backend registered for that mode runs it.
- **interceptor:** any subset of `before_commit(version) -> version | Veto`,
  `after_commit(version)`, `after_hydrate(obj) -> obj`, and `contribute_schema() ->
  fields/columns/indexes`.

A plugin implements **only** the methods for the seams it occupies; unimplemented
hooks are inert. That's what makes "one general mechanism" ergonomic instead of
ceremonial.

---

## 4. Attaching plugins (four ways, one model)

All four resolve to the same thing — adding a provider to a seam — differing only in
scope:

1. **Mixin (per class, most common):** `class Doc(Record, DiffStorage, DurableEvents)`.
   MRO order gives deterministic interceptor ordering.
2. **Class kwargs (per class, sugar for common cases):**
   `class Doc(Record, storage="diff", events="durable")` — resolves names via the
   registry to the same providers.
3. **Global default (per app):** `eventic.use(DurableEvents)` in app setup makes a
   plugin the default for all records that don't override it (e.g. every record durable
   by default in a service).
4. **Entry points (third-party, the generality lever):**
   ```toml
   [project.entry-points."eventic.plugins"]
   encryption = "eventic_encryption:FieldEncryption"
   ```
   Any package can publish a plugin; eventic discovers it by name. This is what makes
   the system *open* — new capabilities without touching eventic's code.

Attachment is **inspectable**: `Doc.__eventic_plugins__` lists the resolved set and
which seam each occupies.

---

## 5. Composition & ordering

- **Exclusive seams (persistence, codec, identity):** at most one provider. Two
  providers for the same exclusive seam → `PluginConflictError` **at class definition**,
  naming both. The default counts as a provider, so choosing `DiffStorage` *replaces*
  `FullSnapshot`; you never silently get two.
- **Delivery registry:** each mode name maps to exactly one backend; registering a
  second backend for an existing mode → conflict. `sync` always exists. Handlers route
  by mode; unknown mode → error at registration.
- **Stacking seams (interceptors):** run in a deterministic order — MRO for mixins,
  `priority` for globals/entry-points, ties broken by registration order. `before_commit`
  runs outer→inner; `after_commit`/`after_hydrate` run inner→outer (symmetric nesting),
  so an `Encryption` plugin can encrypt in `before_commit` and decrypt in
  `after_hydrate` predictably.
- **Isolation:** a failing `after_commit`/`after_hydrate` interceptor is logged and
  isolated (like event handlers); a failing `before_commit` **aborts the write** (it
  runs before durability, so failing loud is correct — no half-written version).
- **Dependency resolution:** the assembler collects every plugin's `requires` and
  checks them against the assembled `provides` set at class definition. Missing a
  requirement (e.g. `DiffStorage` on a `TypedTable`) → clear error naming the unmet
  token. No runtime surprises.

**Everything is validated when the class is created, not when it's imported or first
used.** This directly answers two of the review's findings: the same-name-class crash
and the import-time queue declaration both came from doing registration work at import
time with global side effects. Plugins do their wiring in `__init_subclass__`, scoped
to the class, and fail there with a readable message.

---

## 6. Worked examples (proving generality)

The three from the design conversation, plus four new ones eventic never mentioned —
to show the same mechanism absorbs capabilities the authors never anticipated.

**a. Durable events (the DBOS plugin) — delivery seam.**
```python
class Order(Record, DurableEvents):        # registers the "durable" delivery mode
    total: int
@on_commit(Order, mode="durable")          # enqueued in the SAME txn as the append (outbox)
def charge(order_id): ...                   # id in, re-hydrate, idempotent
```
`requires = {"persistence:transactional"}` (the outbox needs the enqueue to be atomic
with the append). Brings `dbos` into the graph only for classes that use it.

**b. Diff storage — codec seam.**
```python
class Doc(Record, DiffStorage):            # forward deltas + snapshot every K
    body: str
```
`provides={"codec"}`, `requires={"persistence:json"}`. Invisible above `decode` — events
and delivery unaffected (see `CONCEPT.md` §6).

**c. Typed columns — persistence seam.**
```python
class Invoice(Record, TypedTable):         # per-type SQLModel table, indexed columns
    amount: int; status: str
```
`provides={"persistence","persistence:columns","persistence:transactional"}`. Note it
does **not** provide `persistence:json`, so `DiffStorage` + `TypedTable` fails fast with
"codec DiffStorage requires persistence:json" — the incompatibility is caught at
definition, not discovered in production.

**d. Multi-tenancy — interceptor + schema.**
```python
class Doc(Record, MultiTenant): ...        # adds tenant_id column; before_commit stamps it,
                                           # query() scopes every read to the current tenant
```

**e. Encryption-at-rest — interceptor.** `before_commit` encrypts flagged fields;
`after_hydrate` decrypts. Ordering (§5) guarantees encrypt/decrypt bracket correctly.

**f. Soft delete — interceptor.** Adds `deleted_at`; `query()` filters it; a "delete" is
just another version with the flag set (append-only, I1, preserved).

**g. Outbox-to-Kafka — delivery.** A `notify`/`kafka` mode backend publishes each event
externally. Same seam as DBOS; different backend. Handlers written to the mode contract.

None of a–g required changing the core or any other plugin. That is the generality
target.

---

## 7. What plugins may **not** do (enforced guardrails)

The framework refuses, at class-definition time, any plugin that would breach
`CONCEPT.md` §3:

- Cannot make construction or reads write (I2/I3): the assembler only calls persist
  inside `save/update/commit`; interceptors get read-only views outside those.
- Cannot mutate a committed version (I1): `persist` is append; there is no `update_row`
  hook to occupy.
- Cannot pull DBOS/FastAPI into the core graph (I6): only plugins under the `[dbos]`/
  extras import them; the core assembler has no hard reference to any plugin package.
- Cannot fire events pre-commit (I7): `emit` runs strictly after `persist` returns.
- Cannot occupy a stage that isn't one of the five seams: there is no generic
  "monkeypatch any method" hook. If your capability needs one, it's out of scope — a
  deliberate wall.

---

## 8. Keeping it honest (self-critique)

A general plugin system is the textbook "second-system" over-engineering risk. This
design is constrained on purpose so it can't grow into a framework-for-frameworks:

1. **The seam set is closed (five kinds).** New capabilities pick an existing seam or
   are rejected. There is no "add your own seam" — that would be the unbounded
   generality to avoid.
2. **Defaults are plugins.** The core is not "special code + a plugin hook bolted on";
   it's the null plugins wired into the same pipeline. So the framework has exactly one
   code path, exercised by every install, not a rarely-used extension branch.
3. **Fail at definition time, loudly.** No import-time global mutation, no first-call
   surprises — the exact failure modes the review traced to the metaclass/queue design.
4. **No dynamic dispatch magic.** Plugins are ordinary classes resolved by MRO/registry;
   `Doc.__eventic_plugins__` shows the whole truth. No metaclass gymnastics.
5. **Ship two plugins, not ten.** The framework's existence is justified by the concrete
   need for `durable` delivery and (maybe) `diff` storage. The other examples in §6 are
   *demonstrations of reach*, not a roadmap. Build a plugin when a real user needs it;
   the seam being ready is free, the plugin is not.

If, a year in, only `sync` and `durable` delivery ever ship and no third-party plugin
appears, the honest conclusion is that the seam framework should collapse back into two
if-branches. The design is built to make that retreat cheap: because defaults are
plugins and the seams are few, deleting the registry and hardcoding the two backends is
a local change, not a rewrite. **Generality that can be un-generalized cheaply is the
only kind worth shipping.**

---

## 9. Relationship to the other documents

- `CONCEPT.md` defines the invariants (I1–I7) this framework enforces and the pipeline
  stages the seams map onto.
- `TARGET_ARCHITECTURE.md`'s `eventic.dbos` adapter is, in this model, the `DurableEvents`
  delivery plugin plus `create_app`; the "optional `meta` field / typed columns" note is
  the `persistence` seam. Those sections should be re-expressed in plugin terms when the
  target doc is next revised.
- `REIMPLEMENTATION_PLAN.md` Steps 3–6 already isolate DBOS behind an adapter; the plugin
  framework is the generalization of that isolation. Recommended sequencing: land the
  explicit-commit core + `sync`/`durable` delivery first (Plan Steps 1–6), extract the
  seam abstraction only once the second real plugin (`DiffStorage`) needs it — do not
  build the framework before the second plugin exists (guardrail §8.5).
