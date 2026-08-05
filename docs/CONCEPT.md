# Eventic — The Concept (v3)

**Supersedes** `002-reimplementation/CONCEPT.md`. Companion docs: `REVIEW.md` (the
verified evidence that forced this revision), `IMPLEMENTATION_GUIDE.md` (the route).

This document states the **irreducible idea** and the **invariants** that everything
else must respect. If a feature can't be expressed without breaking an invariant
below, it doesn't belong in eventic.

The v2 concept was right about the *idea* and wrong about the *machinery*. §11 records
exactly what changed and why, so the delta is auditable rather than silent.

---

## 1. The one-sentence thesis

> **A plain Pydantic model becomes a versioned aggregate whose version history *is*
> an event stream — persisted with two dependencies (pydantic + SQLAlchemy), and
> nothing else required.**

Unchanged from v2. It survived contact with the implementation; everything below it
did not.

---

## 2. The core realization

1. **A write is an append.** Mutating an aggregate never overwrites; it appends a new
   immutable version. The table of versions is an append-only log.
2. **The log is the event stream.** Every appended version *is* an event: "this
   aggregate is now in this state, as of version N." There is no separate event table
   to keep in sync.
3. **Delivery of those events is a separate concern.** In-process by default; durable
   delivery is an upgrade to the *delivery mechanism*, not a change to what an event
   is.

### 2.1 The realization v2 missed

v2 treated "the write" and "the transaction" as the same thing. They are not, and the
gap between them is where the library's worst bug lived (`REVIEW.md` F3: sync handlers
fired for versions that were later rolled back).

> **A commit is not an append. A commit is a *transaction that contains* an append.**

Once the transaction is the unit — not the row — three things that were separate
features collapse into one mechanism:

- **Event emission** happens when the transaction commits, not when the row is
  inserted. I7 stops being an assertion and becomes a consequence.
- **The durable outbox** is just another row written *inside that same transaction*.
  Atomicity is structural, not a capability token to be checked.
- **The head projection** is a third row written inside that same transaction, which
  is what makes reads fast without making the log less true.

Hence the schema is a triad, not a table:

```
eventic_log      append-only, immutable      — the truth
eventic_head     one row per aggregate       — derived; a cache with the same txn boundary
eventic_outbox   pending durable deliveries  — drained, then reaped
```

`head` and `outbox` are **derived and rebuildable**. Saying so plainly is more honest
than the "single table" story v2 told, which was already fiction the moment durable
delivery existed.

---

## 3. The invariants (non-negotiable)

| # | Invariant | Enforced by |
|---|---|---|
| I1 | **Append-only.** A committed version is immutable; the log only grows. | frozen model + insert-only log writer; `head`/`outbox` declared derived |
| I2 | **No hidden writes.** Only `save` / `update` / `draft().commit()` persist. | no `__setattr__` hooks exist to write through |
| I3 | **Pure construction.** `Todo(...)` is in-memory only, fully validated, no I/O. | frozen model; no I/O in `model_post_init` |
| I4 | **Deterministic version identity.** `version_id = uuid5(NS, "eventic:{id}:{version}")` for every version including v0. | a module function — **not** a seam (§6.1) |
| I5 | **Loud conflicts.** Two different writers at one `(id, version)` raise `StaleVersionError`; only a byte-identical replay is a silent no-op. | unique `(id, version)`; replay compares `data`, which holds **user state only** (§5.1) |
| I6 | **The core imports only pydantic + SQLAlchemy.** | import-graph test in a fresh interpreter |
| I7 | **One commit emits exactly one event, after durability.** | **the transaction emits, not the pipeline** (§4) |
| I8 | **No mutable process state outside a `Store`.** | zero `_reset_*` functions in the package — CI-gated |

I8 is new and it is not bureaucratic. v2 had six module-level mutable globals, each
with a `_reset_*` test hook. Two of the review's architectural findings (a public API
that did nothing; a per-class plugin that was secretly process-wide) were *caused* by
that state model. **Six reset hooks was the codebase reporting its own bug.** The
count is a mechanical proxy for the invariant, so CI can check it.

> If you internalize nothing else: **I1 and I2 are the spine, and I7 is the one that
> is hard to keep.** Everything in §4 exists to make I7 true by construction.

### 3.1 Declarations are not state

I8 forbids mutable *operational* state, not registries populated by class definition
and decoration. Two module-level dicts are permitted and allowlisted:

- `_STREAMS` — stream name → Record class, written by `__init_subclass__`
- `_HANDLERS` — handler id → function, written by `@on_commit`

Both are written by *code loading*, never by *operations*; both raise loudly on
collision rather than silently overwriting. They are the program's shape, not its
state. Everything else lives on a `Store`.

---

## 4. The lifecycle of a write

The pipeline is the shared contract between the core and every seam.

```
construct ─► validate ─► before_commit ─► encode ─► append ─┐
   (I3)      (pydantic)   (interceptors,  (codec)  (log,    │  ONE TRANSACTION
                           may veto)                I1/I4/I5)│
                                          project head ──────┤
                                          stage outbox ──────┤
                                                             │
                                          ══ COMMIT ═════════╡  ◄── durability line
                                                             │
                             after_commit ◄──────────────────┤  (interceptors, isolated)
                             emit ─► deliver ◄───────────────┘  (I7: exactly one event)
```

**The durability line is the whole design.** Nothing to its right may run until the
transaction has actually committed. That is enforced structurally: the pipeline does
not emit — it **stages** events on the unit of work, and the unit of work flushes them
after `COMMIT` returns. When eventic joins a transaction it does not own (a DBOS
workflow, or any caller's session), it binds to that session's `after_commit` event
instead. One mechanism, both paths, no special case.

A byte-identical replay inserts nothing, therefore stages nothing, therefore emits
nothing. "Exactly once per commit" falls out; it does not have to be argued.

## 5. The lifecycle of a read

```
head ────────────────────────► validate ─► after_hydrate ─► object
(one indexed row)                            (interceptors)

log window ─► decode ─► merge ─► validate ─► after_hydrate ─► object
(bounded)     (codec)   (row cols)
```

Head reads (`get(id)`, `where(...)`) hit the projection: one indexed query, the same
cost regardless of codec. Historical reads (`get(id, version=n)`, `history(id)`) read
a **bounded window** of the log — the codec declares how far back it needs, and the
store answers with a single range query. A delta codec never reads more than `K` rows
to answer a point read.

### 5.1 What lives in `data`, and why it matters

`data` holds the codec's output and **nothing else**: user fields only. Managed fields
(`id`, `version`, `version_id`) and commit metadata (`committed_at`, and later `actor`,
`causation_id`) live in columns and are merged back at hydration.

This is not tidiness. It is what makes two invariants coexist:

- `created_ts` must reflect *when the version was committed*, so it cannot be known at
  encode time.
- I5's replay check compares stored bytes, so if a timestamp were inside `data`, a
  crash-recovery replay would produce different bytes and raise `StaleVersionError`
  instead of being the idempotent no-op that makes replay safe.

Splitting state from commit metadata satisfies both. v2 had neither: `created_ts` was
in `data` as a permanent `null` and never worked at all (`REVIEW.md` F5).

---

## 6. The object model

- **`Record`** — a frozen Pydantic `BaseModel` subclass with `extra="forbid"`. Records
  are **values**: an instance is one immutable version, not a mutable entity handle.
  Managed fields: `id`, `version`, `version_id`, `created_ts`, plus a free-form
  `meta: dict`.
- **`Draft`** — the only mutation affordance. `record.draft()` yields a mutable
  scratch copy; `draft.commit()` writes one new version and **returns it**. Every
  write returns the new value; nothing mutates in place.
- **Stream** — the log of one aggregate type. A stream name is declared
  (`class Todo(Record, stream="todo")`), defaults to the class name, and **collides
  loudly**. One stream, one concrete class.
- **Version** — one immutable log row: `(version_id, stream, id, version, kind,
  committed_at, snapshot, data)`.
- **Event** — the fact that a version was committed: `(kind, record, delta)`. Not a
  separately stored object; the outbox stores a *reference* to it, not a copy of it.

### 6.1 Frozen, and why it is not a restriction

v2 shipped a non-frozen model plus a `hair_trigger=True` flag that monkeypatched
`__setattr__` onto the class to re-enable implicit writes. That flag's entire purpose
was to turn off I2. A library cannot both claim "no hidden writes" as invariant #2 and
ship a switch that disables it; one of the two has to go, and it isn't the invariant.

Freezing the model means I1 and I3 are enforced by pydantic rather than by
convention, and it deletes every `object.__setattr__` workaround in the codebase.
`draft()` provides the mutable-feeling ergonomics that `hair_trigger` was reaching for,
without lying about when I/O happens.

---

## 7. The seams

Three seams. Each is a `Protocol` — not a registry entry, not a capability token.

| seam | kind | governs | default |
|---|---|---|---|
| **`RowStore`** | exclusive | where and how rows are stored and queried | `SqlStore` (log + head + outbox) |
| **`Codec`** | exclusive | how a version's state becomes a row's `data` | `Snapshot` (alt: `Delta`) |
| **`Interceptor`** | stacking | `before_commit` / `after_commit` / `after_hydrate` | none |

Seams are selected by **class keyword**, never by inheritance:

```python
class Doc(Record, stream="doc", codec=Delta(k=20), interceptors=(Audit(),)):
    body: str
```

### 7.1 Why keywords and not mixins

`class Doc(Record, DiffStorage)` reads beautifully and is structurally wrong. It puts
a framework class into the user's pydantic MRO, and pydantic's metaclass then collects
the framework's own annotated attributes as model fields. In v2 this silently added
five phantom fields (`seam`, `provides`, `requires`, `priority`, `mode`) to every
plugin-bearing record **and wrote them into the database on every commit** — into an
append-only log, permanently (`REVIEW.md` F1). It also made subclassing a
plugin-bearing record either install a `Record` as the codec or crash at class
definition (F2).

Keywords keep framework types out of the MRO entirely. Config resolves through the
MRO like any other class attribute, so subclassing works the way Python users expect.

### 7.2 Why the capability-token system is gone

v2 had `provides`/`requires` string tokens (`"persistence:json"`) validated by a
bespoke assembler. The only constraint it ever expressed was "a delta codec needs a
JSON-shaped store." That is a **type**, and Python already has a way to say it:

```python
class Delta(Codec):
    requires = JsonRowStore          # a Protocol, checked at class definition
```

Same guarantee (loud failure at class definition, never at import or first call),
zero invented vocabulary, and the type checker enforces it too. `PluginConflictError`
disappears entirely — with keyword selection, two codecs on one class is not an error
to raise, it is a sentence you cannot write.

**The plugin framework should be the type system, not a bespoke registry.**

### 7.3 Identity is an invariant, not a seam

v2 listed identity as one of five seams. `version_id_for` was never called by
anything — `record.py` called the module-level `_uuid5` directly (`REVIEW.md` F9). That
was not an oversight; it was the design self-correcting. I4 says `uuid5(id, version)`
is *the one true identity rule*. A seam you cannot use without breaking a documented
invariant is not a seam. It is a function.

### 7.4 Delivery is a property of the subscription, not of the record class

v2 listed delivery as a seam and had you declare `class Order(Record, DurableEvents)`.
But the mode was *already* selected at the subscription (`on_commit(cls,
mode="durable")`), and the class-level declaration wrote into a process-global
registry that then applied to every record class in the process — including classes
that never opted in and never satisfied the requirement (`REVIEW.md` F10).

The subscription is the right place, and it was always the right place:

```python
@on_commit(Todo, kind="update")                        # inline, post-commit
def log_delta(event): ...

@on_commit(Todo, via="outbox", queue="reindex")        # durable, via the outbox
def reindex(event): ...                                # same Event signature
```

Because the outbox stores a reference to the committed version, a durable handler can
be handed a fully reconstructed `Event` — the same object a sync handler gets. v2's
durable handlers received a bare id string, a signature asymmetry that existed only
because the outbox wasn't real.

---

## 8. Core vs seam — where the line is

The core is the smallest implementation that upholds §3 and runs the §4/§5 pipeline.
Each default is itself the null implementation of its seam:

| seam | default |
|---|---|
| `RowStore` | `SqlStore` — log + head + outbox on one database |
| `Codec` | `Snapshot` — the full validated user state per version |
| `Interceptor` | none |

There is no second extension mechanism. Delivery is not on this table because it is
not a record-class seam (§7.4); dispatchers are ordinary objects you construct.

### 8.1 The anti-sprawl guardrail (retained from v2, and it worked)

v2's `PLUGINS.md` §8.5 said: don't extract the framework until a *second* real
implementation needs it. That guardrail was correct and this revision keeps it, in a
sharper form:

> **Build the general mechanism when the second case arrives, not when you can imagine
> it.**

Applied now, it deletes: the capability-token DSL (one constraint, expressible as a
type), `TypedTable` (a stub that existed only to make one guardrail test pass),
`contribute_schema` (never called), `full_state_rows` (never read), `use()` (never
read — it was a public API that did nothing), and the general query AST (`where()`
needs equality; build the AST when the second predicate kind arrives).

Roughly a third of v2's public surface was speculative. All of it is gone, and the
library gets *smaller* while getting more correct.

---

## 9. What eventic is *not*

- **Not an ORM.** Aggregates are logs of versions, not mutable rows.
- **Not a durable-execution engine.** It emits events and durably records intent to
  deliver them. Queues, retries, and workflows belong to a *dispatcher*, and DBOS is
  one possible driver — no longer the mechanism.
- **Not a full event-sourcing framework.** The log stores state-per-version, not named
  domain intents (`TitleChanged`). Deliberate scope limit.
- **Not polymorphic.** One stream holds one concrete type. Subclassing a `Record`
  creates a new stream with its own log; it does not extend the parent's. If you need
  a family, model it with a union field in one class. (v2 implied polymorphism was
  possible while keying storage on `cls.__name__` and events on the class object —
  two incompatible identity models, `REVIEW.md` F13.)

---

## 10. Positioning

> **eventic — versioned Pydantic aggregates whose history is an event stream.
> Pure-Python core (pydantic + SQLAlchemy); delta storage, durable delivery, and
> alternative stores are opt-in.**

---

## 11. What changed from v2, and why

Every row here is a design change forced by a *verified* defect, not a preference.
Finding ids refer to `REVIEW.md`; each is reproducible via `probes/`.

| # | v2 said | v3 says | Forced by |
|---|---|---|---|
| 1 | Plugins are base classes | Plugins are class keywords | F1 phantom persisted fields; F2 subclassing crashes |
| 2 | Five seams | Three seams | F9 identity seam dead; F10 delivery seam was global |
| 3 | Capability tokens (`provides`/`requires`) | Protocols | never expressed more than one constraint |
| 4 | The pipeline emits | **The transaction emits** | F3 — I7 violated on the DBOS path |
| 5 | One table | Log + derived head + outbox | F16/F17 N+1 reads and full-history point reads |
| 6 | Delivery declared on the record class | Declared on the subscription | F10 leaked to classes that never opted in |
| 7 | Durable handlers receive an id | Receive an `Event` | asymmetry existed only because the outbox was fake |
| 8 | `Record` is not frozen; `hair_trigger` opts out of I2 | Frozen; `hair_trigger` deleted | a switch that disables invariant #2 |
| 9 | `extra="allow"` | `extra="forbid"` | a "loud, never silent" library silently persisting typos |
| 10 | `created_ts` on the row, in `data` | Commit metadata in columns, merged at hydration | F5 — the field never worked; naive fixes break I5 |
| 11 | `edit()` batches into one version | `draft().commit()` returns the new version | F6 — the shipped demo printed the wrong version |
| 12 | Deltas store changed fields | Deltas store changes **and tombstones** | F4 — removed fields resurrected on read |
| 13 | Storage keyed on `cls.__name__` | Explicit stream, loud on collision | F13 — same-named classes silently shared a log |
| 14 | Six process globals + six `_reset_*` hooks | I8: zero | F8/F10 were caused by this |

The thesis (§1) and the core realization (§2.1–2.3) are untouched. **The idea was
never the problem.** Everything that broke was machinery built around it faster than
a second real use case arrived to justify it — which is exactly what v2's own §8.5
guardrail predicted, and exactly the rule §8.1 now applies to itself.
