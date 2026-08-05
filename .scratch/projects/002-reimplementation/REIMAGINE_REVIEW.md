# Eventic — First-Principles Reimplementation Review

**Reviewer role:** senior Python/systems architect, adversarial, with license to
rebuild. **Reviewed:** `main` @ `9a6c2e2` (the completed 8-step 001 refactor).
**Method:** full source read (not docstrings — the code); DBOS 2.29 source in
`.venv`; four live probes in `probes/`. Every claim is tagged **[verified]**
(a probe or the suite proves it) or **[inferred]** (read from code, not executed).
**Baseline:** 28 tests green on SQLite, incl. `-W error`, in ~35s.

> Framing note: the 001 refactor was **good work**. It took a library whose
> headline features crashed the process and made all 28 of them pass. This review
> is not "the refactor was wrong." It is the harder question the refactor was not
> allowed to ask: *given a blank sheet, are these the right abstractions at all?*
> The answer, argued below, is "the storage kernel yes; almost everything wrapped
> around it, no."

---

## Section 1 — First-principles re-derivation (before → after)

Each subsection records my **initial** answer (written after the first read, before
the adversarial pass) and my **revised** answer (after the probes). The delta is the
point.

### 1.1 The job-to-be-done

**Initial:** Eventic is for a solo/small-team dev building a small-to-mid
event-driven Python web app who wants pydantic models that (a) persist without a
hand-written DAO, (b) carry version history for free, and (c) can fan out
background work. The irreducible core looked like "versioned pydantic persistence
+ durable async," a matched pair.

**Revised:** The matched pair is an illusion. The README, the demo, the webhook,
and the test suite all exercise the same 20%: **define a pydantic model → get a
durable, versioned row → read it back by id (and browse its history).** DBOS
durable queues/workflows appear only in the demo's theatrical `end_to_end_demo`
and are used by *zero* of the library's own value propositions. The async story is
a *separate, optional* job that a minority of users hire; the persistence story is
the one everyone hires.
**Therefore our design must** make versioned pydantic persistence excellent and
standalone, and **need not** couple it to — or pay for — a durable-execution engine.

### 1.2 Core abstractions — keep / replace / delete

**Per-attribute copy-on-write (`s.title = x` → new version).**
*Initial:* elegant "hair-trigger"; the signature delight of the library.
*Revised:* **REPLACE with explicit commit.** probe_01: a 4-touch edit session is 4
full-row INSERTs, each preceded by a full-model re-validation; probe_02: two
disjoint concurrent edits silently lose one; the model must be `frozen` and driven
by a metaclass and a bespoke `__setattr__` to fake mutability; the no-op guard
still builds+validates a throwaway object on every assignment. Implicit versioning
conflates "I am *building* an object" with "I am *committing a new fact*." **Design
must** make committing explicit (`.save()`, `.update(**fields)`, or a batched
`with r.edit() as e:` writing one version), which deletes `frozen`, the metaclass's
mutation role, and the surprise-write class of bugs.

**`records` single-table + JSONB `data` **and** `properties`.**
*Initial:* sound event-sourcing-lite; keep as-is.
*Revised:* **KEEP the append-only single table; DELETE the second JSONB bag.**
`data` already contains the full validated model; `properties` is a duplicate blob
(L6) that exists only to host the back-pointer trick. `where()` on it is O(all
latest rows of the class) filtered in Python on SQLite and needs a GIN index you
don't ship on Postgres (M4/§Perf). **Design must** keep id+version+`data`, drop the
separate bag, and treat JSONB search as a documented convenience over `data`, not a
query engine — the primary read path is by id.

**Durable v0 at construction.**
*Initial:* fixes the old "ghost record" bug (C5) — a feature.
*Revised:* **DELETE. Construction must be pure (no I/O).** probe_01: `Story(...)`
is a hidden DB write — a footgun for tests, validation, deserialization, and any
"just build a model" path; and v0's `version_id` is *random* (probe_01[2]) so its
replay story silently differs from every mutation's. **Design must** persist only
on an explicit call, which also erases the v0/vN asymmetry.

**`RecordMeta` metaclass + per-class `Queue` + `@evented`.**
*Initial:* clever; the metaclass is "aha-heavy" but contained.
*Revised:* **DELETE the metaclass and per-class queue.** They cause a real,
user-facing limitation the README has to apologise for — *"two same-named Record
classes cannot coexist in one process"* — purely because a DBOS queue is declared,
keyed on the derived class name, at *import* time (Appendix B documents the
`WebhookStory` rename forced by exactly this). `@evented` pickles the whole Record
(probe_04). **Design must** offer async through a plain, explicit function API
(`enqueue(fn, *ids)`), not class-name-keyed import-time global registration.

**`PropertiesBase` with an `_owner` back-pointer.**
*Initial:* nice ergonomic once H1 was fixed.
*Revised:* **DELETE.** It is a reference cycle (`record.properties._owner is record`),
an aliasing footgun (two hydrated copies of one aggregate carry independent bags),
and it exists solely to make `props.add()` perform a hidden write — the same hidden
write we are removing everywhere else. **Design must** make "extra metadata" either
ordinary typed fields or one explicit `dict` field committed like any other.

**The event system (`on.create`/`on.update`, synchronous, pre-commit).**
*Initial:* a reasonable lifecycle hook.
*Revised:* **REPLACE with post-commit callbacks (or delete).** Update handlers fire
*before* the transaction commits — the README literally instructs users to "treat
the store as eventually-consistent within the emitting transaction," i.e. handlers
can't trust the store they're handed. This is a half-built transactional outbox.
**Design must** either fire callbacks *after* durable commit, or, for users who
have the DBOS adapter, hand off to DBOS's real workflow/notification machinery —
not roll a fragile pre-commit synchronous dispatch.

**DBOS as the substrate.**
*Initial:* the differentiator; durable queues/workflows/recovery justify the dep.
*Revised:* **KEEP DBOS, but as an OPTIONAL adapter, not the substrate.** What DBOS
genuinely buys — durable queues, workflows, exactly-once steps, crash recovery — is
real and worth composing *for the users who need async*. What it costs the *core*
(evidence, not vibes): a mandatory heavy dependency; **~1.1s lifecycle overhead per
process/test** vs 13.8ms of actual work (probe_03); **11 system tables** beside our
one; a global singleton registry that *causes* the same-name-class limitation; a
**pickle** default serializer that is an RCE surface (probe_04); the `sql_session`
assertion dance and the `_session()` fallback that exists only to paper over it;
and version-pinned coupling. For the irreducible core (§1.1), DBOS buys **nothing**.
**Design must** let a user `pip install eventic`, persist versioned models, and read
them back **without DBOS in the import graph**, and reach for `eventic[dbos]` only
when they actually need durable async.

**`Eventic(DBOS)` facade + process-wide singleton.**
*Initial:* convenient "drop-in DBOS."
*Revised:* **DELETE the subclassing.** `Eventic` *is* DBOS's singleton (H3: they're
interchangeable, keyed on the global instance), which fuses the persistence library
to the execution engine and forces once-per-process/`reset()` lifecycle gymnastics
onto everyone — including tests that never touch a queue. Once DBOS is optional this
class disappears; async users talk to DBOS directly through a thin adapter.

### 1.3 What the final public API should look like

**Package layout (core has no DBOS/FastAPI import):**
```
eventic/
  __init__.py         # Record, connect, on_commit, __all__
  record.py           # pure pydantic + explicit save/update/commit (no metaclass)
  store.py            # append-only RecordStore over one SQLAlchemy engine
  models.py           # the single `records` table
  query.py            # get / history / where helpers
  events.py           # post-commit callback registry (sync, in-process)
  dbos/               # OPTIONAL adapter, only imported if installed
    __init__.py       # durable(), queue(), create_app()  -> wraps DBOS
```

**Hello world (5 lines, no DBOS):**
```python
from eventic import Record, connect
connect("sqlite:///app.db")
class Todo(Record):
    text: str
    done: bool = False
t = Todo(text="learn eventic").save()      # pure until .save(); writes v0
```

**Medium app (webhook + a couple of async jobs):**
```python
from eventic import Record, connect
from eventic.dbos import create_app, queue, durable

app = create_app("notes-svc", db_url=DB_URL)     # FastAPI + DBOS, opt-in
class Note(Record):
    title: str | None = None
    body: str | None = None

@durable                                          # a real DBOS step
def reindex(note_id): Note.get(note_id)           # pass ids, re-hydrate

@app.post("/webhook")
async def hook(payload: NoteIn):
    note = Note(title=payload.title, body=payload.body).save()
    queue("notes").enqueue(reindex, note.id)      # explicit, id-only arg
    return {"id": str(note.id)}
```

**Power user (history, migrations, multi-class):**
```python
Note.get(nid)                     # latest
Note.get(nid, version=3)          # exact version (loud KeyError if absent)
list(Note.history(nid))           # oldest→newest
Note.where(status="published")    # JSONB convenience over data (documented cost)
n2 = Note.get(nid).update(body="edited")   # returns the new version; original untouched
```

**Therefore the library's positioning must change** from *"Pydantic on a
hair-trigger + DBOS"* to *"versioned, persistent pydantic records; bring your own
async."* (Naming discussion in TARGET_ARCHITECTURE.md §7.)

---

## Section 2 — Findings by dimension

Severity = impact on a real user of a public MIT library. New IDs prefixed `R`
(reimagination) to avoid collision with the 001 `C/H/M/L` set.

### 2.1 Correctness & data integrity

**R-C1 [Critical] [verified] — The "concurrency contract" is a silent lost-update.**
`probe_02`. Two writers derived from the same version edit *disjoint* fields; the
second write computes the same deterministic `(id, version)`, is swallowed by
`ON CONFLICT DO NOTHING`, and disappears **with no error**, while its in-memory
object continues to report the value it "wrote." The README (§Concurrency contract)
sells this exact mechanism as *"the history never corrupts."* It is true that no
duplicate *row* is written — but a *logical* update is destroyed. `store.py:82-89`.
The deterministic-key idempotency (great for crash-replay) is indistinguishable from
last-write-loses (terrible for concurrency); the design cannot tell them apart. In a
standalone script there is no SERIALIZABLE retry to rescue it.
*Impact:* undetectable data loss under any real concurrency in the marketed
"script-friendly" path. **This is the finding that most damages the current design.**

**R-C2 [High] [verified] — v0 idempotency is not actually idempotent.**
`record.py:61` gives `version_id` a random `uuid4` default; only `__setattr__`
(`record.py:111`) overrides it deterministically. probe_01[2] confirms v0's stored
`version_id` is random. So the crash-recovery idempotency the README promises
("re-run inserts the *same* row") holds for mutations but **not for construction**:
a replayed v0 either relies on DBOS transaction atomicity (fine inside a txn) or, in
a script, can double-insert a logically-identical v0 under a new PK (only the
`UNIQUE(id, version)` saves it — and only because `version` is 0, not because the
key is stable). An asymmetry the docs don't mention. `record.py:61,69-92`.

**R-C3 [High] [inferred] — No identity map: two hydrations of one aggregate diverge
silently.** `hydrate` returns a fresh object each call (`record.py:165-193`); there
is no per-transaction identity map. probe_02 shows the concrete consequence: writer
B holds a stale object that believes it committed. Even single-threaded,
`a = Note.get(x); b = Note.get(x); a.update(...)` leaves `b` stale with no signal.
For a library whose entire pitch is aggregates, the absence of aggregate identity is
a structural gap.

**R-C4 [Medium] [inferred] — Event timing is a documented footgun, not a design.**
`update` handlers fire in `_commit` (`record.py:146-149`) *before* the enclosing DBOS
transaction commits (H6 timing, README §Event handlers). The library ships a hook
whose contract is "the store may not reflect what you were just handed." That is a
transactional-outbox problem solved by *asking the user to tolerate it*.

**R-C5 [Low] [inferred] — `_session()` fallback conflates "no transaction" with
"inside a DBOS step."** `store.py:54-57` catches `AssertionError` from
`DBOS.sql_session` and treats *any* such failure as "standalone." Inside a DBOS
**step** (not transaction) the same assertion fires, so a step silently writes via a
*separate* engine session outside the step's durability envelope — the opposite of
what a user in a `@DBOS.step` would expect. The boundary is a bare `except
AssertionError`, not an intentional context check.

### 2.2 Security

**R-S1 [High] [verified] — Pickle is the queue/workflow arg serializer (RCE
surface).** `probe_04`: DBOS 2.29's default serializer is `py_pickle`; a crafted
serialized arg runs arbitrary code on `deserialize()`. `@evented`
(`dispatcher.py:46-47`) enqueues the **whole Record** (504 b64 chars) as a pickled
arg. Any path where enqueue arguments, or the DBOS system database, are influenced
across a trust boundary is a remote-code-execution vector. The current library does
not constrain enqueue args to ids/JSON and does not document the risk.
*Mitigation direction:* constrain enqueue args to ids + JSON-serializable values; or
configure DBOS's `DBOSPortableJSON` serializer; or, in the core, don't ship a queue
at all (R-arch below).

**R-S2 [Low] [inferred] — Webhook is hardened but `Record` is `extra="allow"`.**
`WebhookPayload` (`main.py:57-65`) correctly refuses reserved fields — good. But
`Record.model_config = {"extra": "allow"}` (`record.py:66`) means any *other*
`Record` constructed from semi-trusted dicts silently absorbs arbitrary keys into
`__pydantic_extra__`, which are then persisted and reflected. The webhook is safe by
virtue of a hand-written DTO, not by the model being safe.

### 2.3 API ergonomics & footguns

A dozen things a new user trips on (each [verified] by code/probe unless noted):

1. `Todo(text="x")` **writes to the database** (probe_01) — construction has a side
   effect on shared state.
2. Constructing a model *before* `Eventic.init()` silently **doesn't** persist but
   still fires `on.create` (`record.py:87-92`) — same call, two behaviours.
3. `s.title = x` **also** writes; there is no way to batch edits into one version.
4. `s.version = 1` raises `AttributeError` (`record.py:98-101`) — a field that looks
   assignable isn't.
5. `props.add(...)` writes a new version; `props.status = x` (plain attr set) does
   **not** — two ways to touch the bag, one persists.
6. Same-name `Record` subclasses **crash the process** at import (queue registry);
   the README documents the workaround (rename to `WebhookStory`).
7. `Eventic.init()` is once-per-process; a second call raises; tests must
   `reset()`/`destroy()` — heavy ceremony for a persistence library (probe_03).
8. `hydrate(id, at_version=3)` returns "latest ≤ 3," *not* version 3 — silent older
   object if v3 is missing (`record.py:186-192`).
9. `where()` values must be JSON-normalizable; `_jsonable` handles UUID/datetime but
   nothing else (`store.py:28-34`) — a `Decimal` or `Enum` filter silently mismatches.
10. Two live copies of one aggregate diverge silently (R-C3).
11. A concurrent edit can vanish silently (R-C1).
12. `on.update` handlers see pre-commit state (R-C4).

*Impact:* the surface violates least-surprise repeatedly, and every surprise traces
back to one root cause — **actions that look like pure in-memory operations
(`construct`, `=`, `.add`) perform hidden, order-dependent database writes.**

**R-E1 [High]** captures items 1/3/5 together: *hidden writes are the central
ergonomic defect.* Fixing it (explicit `save/update`) collapses ~half this list.

### 2.4 Performance

**R-P1 [Medium] [verified] — Write amplification.** probe_01: construct + 2 edits +
1 `props.add` = 4 full-row INSERTs, each preceded by constructing and validating an
entire new model (`record.py:115`, `properties.py:49-59`). A form with 6 fields
edited one-by-one is 6 versions and 6 revalidations. The no-op guard avoids the
*insert* but not the *revalidation* (probe_01[4]).

**R-P2 [Medium] [verified] — `where()` is an O(all-latest-rows) Python scan on
SQLite.** `store.py:145-154`: on non-Postgres it fetches every latest row of the
class and filters in Python (`_dict_contains`); on Postgres it uses `jsonb @>` but
the repo ships **no GIN index** on `properties`, so it's a seq-scan there too.
Marketed as a query feature; it is a table scan.

**R-P3 [Medium] [verified] — DBOS lifecycle dominates every run.** probe_03: ~1.1s of
init+launch+destroy per process vs 13.8ms of real work; the 28-test suite spends
~98% of its 35s in DBOS lifecycle. Every plain script pays 11 system-table
migrations to store one row.

### 2.5 Testability, maintainability, architecture

**R-M1 [High] [inferred] — Hidden global state is pervasive.** `Record._store`
class var (injected by mutation of `Record.__subclasses__()`, `bootstrap.py:34-36`),
the `Eventic` singleton + DBOS global registry, per-class `cls.queue` created at
import, `PropertiesBase._owner` back-pointers, the module-global `_registry`. Six
distinct globals for a library that fundamentally maps "(id, version) → JSON." Each
is a source of test-ordering coupling (see `test_webhook.py`'s comment about sharing
the per-process registry).

**R-M2 [Medium] [inferred] — The metaclass is aha-heavy and now nearly vestigial.**
Post-refactor `RecordMeta` (`record.py:32-53`) does two things: name the queue and
wrap `@evented` methods. Both exist only to serve the per-class queue, which itself
exists only to serve `@evented`, which almost nobody uses. A plain base class + a
free `enqueue()` function deletes the metaclass entirely.

**R-M3 [Medium] [inferred] — The test suite tests implementation, not behaviour.**
Several tests encode the *current* mechanism as the contract: e.g.
`test_concurrent_mutations_do_not_duplicate_versions` asserts `fresh.title in
("from A","from B")` — which **passes even though a write was silently lost**
(R-C1). A behavioural test would assert that a lost update is *either* merged *or*
raises — the current suite blesses the bug. `test_replayed_append_is_idempotent`
reaches into `Story._store.append` internals.

**R-M4 [Low] [inferred] — Can a newcomer hold it in their head?** Almost — the
modules are small and cleanly separated (a genuine strength). But three of the
"aha" mechanisms (metaclass wrapping, `_owner` back-references, copy-on-write via
`frozen`+`__setattr__`) must all be understood *together* to predict what `s.x = 1`
does. Deleting them is a net comprehension win.

### 2.6 Ecosystem & redundancy

**R-X1 [High] [inferred] — Most of the surface is reconstructable from boring
tools, and the parts that aren't are the parts to keep.** Point-for-point:
- versioned rows → `sqlalchemy-continuum`, `eventsourcing`, or the very table
  eventic already has;
- durable queues/workflows → DBOS itself, `temporalio`, or `arq`/`dramatiq`/`huey`;
- pydantic ↔ SQL → `SQLModel`;
- fan-out notify → Postgres `LISTEN/NOTIFY`.
What eventic **uniquely** adds is the *pydantic-native, 5-line ergonomics* of "define
a model, get versioned persistence." That thin ergonomic layer is the whole reason
to exist — and it's exactly what the current design buries under DBOS, a metaclass,
and a properties bag.

**R-X2 [Medium] — Features kept "because the README says so."** The `@evented`
double-life, the properties bag, and the synchronous event system are each in the
public surface because they were in v0.1, not because a user needs them. None is
exercised by the core value proposition; each carries a top-tier finding above.

---

## Section 3 — The verdict

### The recommendation: **thin rewrite** (repositioned core + optional DBOS adapter).

Not "keep with adjustments" (the criticals are architectural, not local), and not
"full rewrite" (the storage kernel is genuinely good and the migration cost of
throwing it away is unjustified). Concretely: **keep** the append-only `records`
table, `id`+`version`, deterministic *mutation* `version_id`, and `hydrate`/history
reads; **rewrite** `record.py` around explicit `save()/update()/commit()` with a
pure constructor and no metaclass; **fold** properties into fields/one dict;
**demote** DBOS to `eventic[dbos]`; **delete** the `Eventic(DBOS)` subclass, the
per-class queue, `@evented`-as-magic, and the `_owner` back-pointer. Full target in
TARGET_ARCHITECTURE.md; ordered steps in REIMPLEMENTATION_PLAN.md.

### Strongest argument **for** this verdict

Every one of the top findings — silent lost update (R-C1), hidden writes (R-E1),
write amplification (R-P1), the pickle RCE surface (R-S1), the same-name-class crash
(§2.3.6), the 1.1s/run tax (R-P3) — is a *direct consequence* of two choices:
"persist implicitly on every attribute touch" and "sit the whole library on DBOS."
Reversing exactly those two choices deletes the findings *and* deletes code
(metaclass, frozen hack, back-pointer, singleton, `_session()` fallback). A smaller
library results that does the core beautifully — which is the stated design value.
The storage kernel and ~half the modules survive, so the cost is bounded and the
migration is boring (one additive column-compatible schema; a shim that keeps the
old `s.x = y` working through a deprecation window).

### Strongest argument **against** (fiercely, per the mandate)

Two honest counters:

1. **"The hair-trigger *is* the product."** Explicit `save()/update()` turns eventic
   into "an ORM with history," a crowded field (`SQLModel` + `sqlalchemy-continuum`,
   `eventsourcing`). The magic `s.title = x → persisted` is the one thing that makes
   eventic *feel* different, and it's plausibly why its author (likely its primary
   user) built it — for delightful scripting, not for high-concurrency services
   where R-C1 bites. Removing the magic is a real ergonomic regression *for that
   user*. **Rebuttal:** the magic and the criticals are the same coin; you cannot
   keep implicit-write and also fix silent data loss, hidden writes, and
   amplification. The compromise (kept in the plan) is a `with r.edit() as e:`
   batch-write and an *optional* `hair_trigger=True` mode that preserves the old
   feel behind an explicit opt-in — magic for scripts, safety by default.

2. **"Then why not just delete the library?"** If DBOS becomes optional and
   properties become fields, what remains is versioned-pydantic-persistence, which
   `eventsourcing` and `sqlalchemy-continuum` already do. This deserves a straight
   answer, not a dodge. **Rebuttal:** none of those is pydantic-native with a 5-line
   onboarding; `eventsourcing` imposes its own aggregate/event DSL, Continuum imposes
   SQLAlchemy ORM models. The unique, defensible value is *"a plain Pydantic v2 model
   becomes a versioned, persisted aggregate in five lines, and can opt into DBOS
   durability when it grows up."* That is worth ~600 lines of well-tested code. It is
   **not** worth the current 900+ lines of metaclass/singleton/back-pointer/pickle
   machinery. So: not delete — **shrink to the defensible core.**

### What I'd be wrong about

If real telemetry showed that (a) users overwhelmingly *do* use `@evented`/queues
and (b) they run single-writer-per-aggregate so R-C1 never fires, then "keep with
adjustments + fix R-S1/R-P3" would beat a rewrite. I have no such telemetry; the
*code's own* evidence (demo-only queue usage, README apologies, the tests that bless
the bug) points the other way. The plan's first three steps are therefore designed
to be reversible and to keep the suite green at each commit, so the rewrite can be
abandoned cheaply if that telemetry appears.

---

*Evidence index:* R-C1→probe_02 · R-C2/R-P1→probe_01 · R-P3→probe_03 · R-S1→probe_04
· all others → cited file:line + the green suite. See `probes/` for runnable scripts.
