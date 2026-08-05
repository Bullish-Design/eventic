# Eventic — The Concept

Companion docs: `REIMAGINE_REVIEW.md` (why), `TARGET_ARCHITECTURE.md` (the target
surface), `REIMPLEMENTATION_PLAN.md` (the route), `PLUGINS.md` (the extension
framework this concept is designed to host).

This document states the **irreducible idea** and the **invariants** that everything
else — including every plugin — must respect. If a feature can't be expressed
without breaking one of the invariants below, it doesn't belong in eventic.

---

## 1. The one-sentence thesis

> **A plain Pydantic model becomes a versioned aggregate whose version history *is*
> an event stream — persisted with two dependencies (pydantic + SQLAlchemy), and
> nothing else required.**

Everything past that sentence — durable async, diff storage, typed columns, notify
fan-out — is optional and lives behind the plugin seams defined in `PLUGINS.md`.

---

## 2. The core realization

The current library treats *versioning*, *events*, and *durable execution* as three
features bolted together on top of DBOS. They are not three features. They are one
idea and one optional add-on:

1. **A write is an append.** Mutating an aggregate never overwrites; it appends a new
   immutable version. The table of versions is an append-only log.
2. **The log is the event stream.** Every appended version *is* an event: "this
   aggregate is now in this state, as of version N." There is no separate event
   table to keep in sync — the version you just wrote is the event you just emitted.
3. **Delivery of those events is a separate concern.** By default they're delivered
   synchronously, in-process, right after the write commits. Durability, retries, and
   background execution are an *upgrade to the delivery mechanism*, not a change to
   what an event is. That upgrade is where — and the only place where — a durable
   engine like DBOS is justified.

That collapse (three features → one log + a pluggable delivery) is the whole design.
The name finally means what it says: **event-ic** = aggregates defined by their
event/version history.

---

## 3. The invariants (non-negotiable)

These hold in the core and are **enforced against every plugin**. A plugin that
violates one is rejected at class-definition time (see `PLUGINS.md` §6).

| # | Invariant | Why it exists |
|---|---|---|
| I1 | **Append-only.** A committed version is immutable; writes only add versions. | The log's integrity; time-travel reads; audit. |
| I2 | **No hidden writes.** Constructing or reading never touches the database; only explicit `save`/`update`/`commit`/`edit` persist. | Kills the review's central footgun class (probe_01/R-E1). |
| I3 | **Pure construction.** `Todo(...)` is in-memory only, fully validated, no I/O. | Tests, validation, deserialization, background threads. |
| I4 | **Deterministic version identity.** `version_id = uuid5(NAMESPACE, "eventic:{id}:{version}")` for **every** version, including v0. | Replay-idempotency without asymmetry (R-C2). |
| I5 | **Loud conflicts.** Two different writers at the same `(id, version)` raise `StaleVersionError`; only a byte-identical replay is a silent no-op. | Kills silent lost-update (probe_02/R-C1). |
| I6 | **The core imports only pydantic + SQLAlchemy.** No DBOS, no FastAPI, no queue lib in the core import graph. | The 80% user pays nothing for the 20% capability (probe_03/R-P3). |
| I7 | **One commit emits exactly one event, after durability.** Events fire post-commit, never pre-commit. | Handlers can trust the store (R-C4). |

If you internalize nothing else: **I1 (append-only) and I2 (no hidden writes) are the
spine.** Every plugin is measured against them first.

---

## 4. The object model

- **`Record`** — a Pydantic `BaseModel` subclass. Not frozen, no metaclass. Carries
  four managed fields: `id` (stable aggregate identity), `version` (monotone int),
  `version_id` (deterministic PK, I4), `created_ts`. Everything else is the user's
  domain fields, plus an optional free-form `meta: dict`.
- **Version** — one immutable row in the log: `(version_id, id, version, class_type,
  created_ts, <encoded state>)`. "Encoded state" is produced by the **codec** seam
  (default: the full validated snapshot; optional: a diff — see `PLUGINS.md`).
- **The log** — the append-only set of versions for an aggregate `id`, ordered by
  `version`. Reading = selecting from it and decoding.
- **Event** — the fact that a version was committed. Its payload is the new version
  (or, for update events, optionally the delta). Not a separate stored object.

---

## 5. The lifecycle of a write (the canonical pipeline)

This pipeline is the **shared contract** between the core and every plugin. Plugins
attach at the named stages; the core provides the default at each stage. (Stage
names are referenced by `PLUGINS.md`.)

```
construct ─► validate ─► before_commit ─► encode ─► persist ─► after_commit ─► emit ─► deliver
   (I3)       (pydantic)   (hooks, may     (codec)   (append,    (hooks; post-  (one   (backend:
                            veto/enrich)              I1/I4/I5)   durable, I7)  event) sync default)
```

- **construct / validate** — pure, in-memory, no I/O (I3).
- **before_commit** — stacking hooks may inspect or enrich the pending version (e.g.
  stamp `actor_id`, encrypt a field) or veto it (e.g. access control). Cannot cause a
  hidden write.
- **encode** — the exclusive codec turns (prev, new) into the row's stored bytes.
- **persist** — the exclusive persistence strategy appends the row, upholding I1/I4/I5.
- **after_commit** — stacking hooks run only once the row is durable (audit, metrics).
- **emit → deliver** — exactly one event (I7), handed to the selected delivery
  backend (default `sync`, in-process; `durable` via the DBOS plugin).

## 6. The lifecycle of a read

```
select ─► decode ─► after_hydrate ─► object
(persist  (codec    (hooks; e.g.
 -ence)   replays)   decrypt, redact)
```

`get(id)` / `get(id, version=n)` / `history(id)` / `where(**eq)` all follow this
path. Because `decode` always yields a **fully reconstructed, validated object**,
nothing above the codec seam knows or cares how the version was stored. That
invisibility is what lets diff-storage and snapshot-storage coexist without the
event or delivery layers noticing.

---

## 7. Core vs plugin — where the line is

The **core** is the smallest implementation that upholds §3's invariants and runs
the §5/§6 pipeline with a default at every stage. Crucially, **each default is
itself the "null plugin" for its seam**:

| seam | core default (the null plugin) |
|---|---|
| persistence | single append-only `records` table, JSONB state |
| codec | full validated snapshot per version |
| delivery | synchronous, in-process, post-commit |
| identity | `uuid5` deterministic (I4) |
| interceptors | none |

There is no special-casing: the core is "eventic with the default plugins wired in."
Swapping any default for another provider is the *only* extension mechanism, which is
why the plugin framework can be small and uniform (`PLUGINS.md`).

---

## 8. What eventic is *not*

- **Not an ORM.** Aggregates are logs of versions, not mutable rows. If you want live
  mutable rows with typed columns, that's the optional `TypedTable` persistence
  plugin — a deliberate choice, not the default.
- **Not a durable-execution engine.** It emits events; a delivery plugin (DBOS) can
  make them durable. eventic does not reimplement queues, retries, or workflows.
- **Not a full event-sourcing framework.** The log stores state-per-version, not
  named domain intents (`TitleChanged`). That's a deliberate scope limit: reacting to
  "the new state" covers eventic's job; rich intent modeling is out of scope.

Stating the non-goals is how the library stays the size the review demands: a small
core that does versioned-events beautifully, and a plugin seam for everything else.

---

## 9. Positioning

> **eventic — versioned Pydantic aggregates whose history is an event stream.
> Pure-Python core (pydantic + SQLAlchemy); durable async, diff storage, and typed
> columns are opt-in plugins.**

This replaces the current "Pydantic on a hair-trigger + DBOS" framing, which
advertised the two behaviours (implicit writes, mandatory DBOS) that generated the
library's worst bugs.
