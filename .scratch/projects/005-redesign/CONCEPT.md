# Eventic — The Concept (v1.0)

**Status:** proposed ground-up redesign. Supersedes `002-reimplementation/CONCEPT.md`
and `003-structural-refactor/CONCEPT.md`.
**Companions:** `ARCHITECTURE.md` (the shape), `IMPLEMENTATION_GUIDE.md` (the route).
**Evidence base:** `003-structural-refactor/REVIEW.md` (23 verified findings against
0.2) and `004-structural-refactor-review/REVIEW.md` (32 verified findings against
0.3). Finding references below are written `003/F4`, `004/F01`.

**Precondition:** there is no live data. This is a fresh 1.0 with no compatibility
surface, no migration path from 0.x, and no deprecation shims.

---

## 1. The one-sentence thesis

> **A plain Pydantic model becomes a versioned document whose revision history is
> the log, and whose commits are a transactional change feed.**

The thesis has survived three implementations unchanged. It was never the problem.

### 1.1 The honest positioning

Say what it is, precisely, and the scope limits stop being apologies:

> **eventic — a versioned document store with transactional change notification.**

Not "event sourcing lite." The log stores *state per revision*, not named domain
intents (`TitleChanged`). That single fact explains every scope limit in §4: no
polymorphism, no projections DSL, no domain-event vocabulary, no aggregate
reconstruction from intents. A versioned document store is a small, real, useful
thing. An event-sourcing framework is not what this is, and pretending otherwise is
what pulled the previous three versions toward machinery they could not keep correct.

---

## 2. The one cause

Three reviews produced roughly seventy findings. 003 grouped them into three root
causes; 004 into four required structural changes. Both stopped one level short.
There is a single cause underneath all of it:

> **`Record.save()` is ActiveRecord, and ActiveRecord requires ambient global state.**

Every structural defect in 0.1–0.3 is downstream of wanting `todo.save()` to work
without naming a database:

| Consequence of ambient state | Findings |
|---|---|
| A process-global "current store" — `_ENGINE`, then `_CURRENT`, then activation tokens | 003/F8, 003/F10, 004/F07, 004/F16 |
| Lifecycle fields on the user's model, therefore client-settable | 004/F02, 004/F06 |
| Framework config resolved through the user's class (`__init_subclass__`, MRO, mixins) | 003/F1, 003/F2, 004/F22 |
| Import-time registries, because a decorator must find a class without an owner object | 003/F13, 003/F22, 004/F13, 004/F26 |
| A pipeline that *sequences* store calls, because no object owns the transaction | 003/F3, 004/F01, 004/F03, 004/F07 |
| Six `_reset_*` hooks — the codebase reporting its own bug | 003 root cause #2 |

The fix is not a better registry, a compiler, or a plan object. The fix is to delete
the affordance that requires them: **operations move off the value and onto a
store-bound handle.** Once `save()` is gone, there is nothing to make ambient.

### 2.1 The corollary that shrinks the library

If the user's state is a plain `BaseModel` that eventic never subclasses, then the
extension mechanism for a library of plain Pydantic models is **ordinary Python
composition**. The write path is a method call the caller already controls; a
normalizer is a validator; a policy is an `if`; an observer is a function you call.
The interceptor stack, the seam registry, the capability tokens, the plugin base
class, the extension-bundle protocol, and the application compiler are all answers to
a question that only exists while the framework owns the user's class.

**1.0 has exactly two extension points: the `Store` protocol and subscription
handlers.** Everything else is deleted, not deferred to a plugin system.

---

## 3. Vocabulary

One word per concept, used nowhere else. The word `Record` is retired permanently —
it is the word that keeps luring the design back into ActiveRecord.

| Term | Meaning |
|---|---|
| **state** | The user's plain `pydantic.BaseModel` instance. eventic never subclasses it. |
| **`Stream[T]`** | A declaration: a name, a state type, a schema version, an upcaster chain. Immutable value, no side effects. |
| **aggregate** | One versioned document. Its key is `(stream, id)`. |
| **revision** | One immutable committed state of an aggregate. Numbered from 0. |
| **`Revision[T, M]`** | The envelope: `stream`, `id`, `revision`, `revision_id`, `state: T`, `meta: M`, `committed_at`. A generic Pydantic model. |
| **`Commit[T, M]`** | The change-feed envelope handed to handlers: `kind`, `revision: Revision[T, M]`, `changed: frozenset[str]`. |
| **canonical document** | The one authoritative JSON serialization of a revision's state. Everything derives from it. |
| **digest** | `sha256` of the canonical document. The identity of a revision's *content*. |
| **encoding** | How the canonical document is physically stored: `snapshot/1` or `delta/1`. A closed set. Never authoritative for logical state. |
| **`App`** | An immutable, validated declaration: id, streams, meta type, subscriptions. Not a registry, not a service locator, not a compiler. |
| **`Store`** | A backend that implements atomic commit and exact reads. Two implementations: SQLite and PostgreSQL. |
| **`Runtime`** | `app.bind(store)`. The only object through which anything is read or written. |
| **`Collection[T]`** | `runtime[stream]`. Where `create` / `change` / `replace` / `get` / `history` / `where` live. |
| **intent** | A durable row recording that a subscription owes a delivery. |

Deleted words: `Record`, `Draft`, `codec`, `seam`, `plugin`, `interceptor`,
`hair_trigger`, `connect()`, `version` (as a noun for a revision — `schema_version`
keeps the word for its real meaning).

---

## 4. The invariants

Each invariant names the mechanism that makes violating it *impossible to write*,
not the test that would catch it. An invariant enforced by a test is a convention.

| # | Invariant | Made true by |
|---|---|---|
| **I1** | **Append-only.** A committed revision is never modified or deleted. | The store exposes no update or delete path for log rows. Production guidance grants the application role `INSERT`-only on the log table. |
| **I2** | **The log is the only truth.** `head`, intents, and any projection are derived and byte-exactly rebuildable from the log. | Every log row carries the `digest` of its logical document; `eventic verify` recomputes heads from the log and compares digests. A rebuild that changes an observable byte is a failing test, not an operator surprise. |
| **I3** | **One canonical document.** For each revision there is exactly one canonical byte string, and the log row, the head row, the returned `Revision`, and the emitted `Commit` all derive from *it*. | The commit path serializes once. The store derives the head by decoding the row it just encoded. Nothing is computed twice from two sources. |
| **I4** | **Pure declaration.** Constructing state, a `Stream`, a `Subscription`, or an `App` performs no I/O and touches no global. | No module-level mutable state exists in the package. Declarations are frozen values validated in their constructors. |
| **I5** | **Explicit, store-bound writes.** Persistence happens only through a `Collection` obtained from a `Runtime` bound to a `Store`. | There is no ambient store, no `ContextVar`, and no method on the state model. `todo.save()` is not a sentence you can write. |
| **I6** | **Deterministic identity.** `revision_id = uuid5(NS, f"{stream}:{id}:{revision}")`. The aggregate key is `(stream, id)` in uniqueness, identity derivation, head keys, replay comparison, and every error message. | One module function, used everywhere; `(stream, aggregate_id, revision)` is the unique constraint. |
| **I7** | **Loud conflicts.** Every write carries an `expected_revision`, compared against the durable head *inside* the commit transaction. Mismatch raises `RevisionConflict`. A write is a silent no-op only when the target row already exists **and** every durable field matches — including stream, kind, schema version, meta, and digest. | Compare-and-swap in the store, not an application-level check. Replay comparison is on the digest, never on a JSONB round trip. |
| **I8** | **Atomic commit.** The log row, the head row, and every delivery intent for a commit are written in one transaction, or none of them are. | One store method, `commit(batch) -> results`. The orchestration layer cannot pair one backend's session with another's methods, because it never sees a session. |
| **I9** | **Post-durability dispatch, honestly named.** One *commit intent* per appended revision. Inline handlers are best-effort, in-process, after `COMMIT` returns. Outbox handlers are durable **at-least-once** and must be idempotent. | Inline dispatch is unreachable before the store's `commit` returns. The word "exactly once" does not appear in the documentation. |
| **I10** | **Decodable history.** Every log row records `schema_version` and `encoding`, sufficient to decode it without today's class being the only possible decoder. | Both are non-null columns with check constraints; the upcaster chain is validated at `App` construction; `eventic schema check` compares stored fingerprints to declared ones. |

> If you internalize nothing else: **I3 and I8 are the spine.** Every release blocker
> in 004 is an instance of computing the same thing twice (I3) or failing to make the
> writes atomic and store-scoped (I8).

---

## 5. The lifecycle of a write

```
caller                                                             purity
──────────────────────────────────────────────────────────────────────────
col.change(rev, done=True)
  1  validate      prev.state ⊕ changes  ──► T                      pure
  2  canonicalize  strip computed ► sort keys ► utf-8 ► bytes       pure
  3  verify        validate_json(bytes) re-canonicalizes identical  pure
  4  digest        sha256(bytes)                                    pure
  5  plan          CommitRequest(stream, id, expected_revision,     pure
                   kind, schema_version, payload, meta, digest,
                   intents)
──────────────────────────────────────────────────────────────────────────
  6  store.commit([request])                              ◄── THE ONLY I/O
        ┌ one transaction ────────────────────────────────────────┐
        │  a  compare-and-swap on (stream, id, expected_revision) │
        │  b  encode payload per the stream's encoding            │
        │  c  INSERT log row       (unique (stream, id, revision))│
        │  d  UPSERT head FROM THE DECODED ROW  ─ not from (1)    │
        │  e  INSERT delivery intents                             │
        │  f  COMMIT ────────────────────────────── durability line│
        └─────────────────────────────────────────────────────────┘
──────────────────────────────────────────────────────────────────────────
  7  materialize   Revision[T,M] from the SAME bytes + committed_at  pure
  8  return                                                          pure
  9  dispatch      inline handlers, best-effort, after COMMIT        I/O (user)
```

Three properties fall out rather than being argued:

- **Step 6d is the whole of I2/I3.** The head is written by decoding the row that was
  just encoded. A buggy encoding cannot make the head and the log disagree — it makes
  the *commit* fail, loudly, at write time. (004/F01, 004/F11.)
- **The write path is exactly one round trip.** Nothing between steps 1 and 5 touches
  I/O, so nothing between steps 1 and 5 acquires a color. This is what makes async a
  later additive change rather than a fork (§9).
- **No read precedes the write.** The caller already holds `rev.state`; the
  compare-and-swap in 6a makes a prior read unnecessary and a stale handle safe — it
  fails loudly instead of creating a gap. (004/F02.)

## 6. The lifecycle of a read

```
head row ──────────────► upcast ─► validate ─► Revision[T, M]
(one indexed lookup)      (I10)     (pydantic)

log window ─► decode ─► upcast ─► validate ─► Revision[T, M]
(bounded by encoding)   (I10)     (pydantic)
```

`get(id)` and `where(...)` read the head: one indexed query, the same cost for every
encoding. `get(id, revision=n)` and `history(id)` read a **bounded window** of the log
— the encoding declares how far back it needs and the store answers with one range
query. Hydration is pure and shared by both paths, so a head read and a log read of
the same revision are provably the same object.

---

## 7. What is sealed and what is open

### 7.1 Sealed — the kernel

Not extensible, by design. Nothing may wrap, replace, or observe the interior of:

- aggregate identity `(stream, id)` and revision identity;
- create-versus-change transitions and revision contiguity;
- compare-and-swap and replay semantics;
- canonicalization, round-trip verification, and digest computation;
- the rule that log, head, return value, and event derive from one canonical document;
- atomicity of log + head + intents;
- the durable delivery state machine;
- `committed_at` as the database's UTC clock at the durability boundary.

### 7.2 Open — the two extension points

| Extension point | Contract | Implementations at 1.0 |
|---|---|---|
| **`Store`** | Atomic commit, exact reads, intent claim/settle. Proven by a published conformance suite, not by a `Protocol` shape check. | `SQLite`, `Postgres` |
| **Subscription handler** | `(Commit[T, M]) -> None`, delivered `Inline()` or via `Outbox(queue=...)`. | user functions |

Two backends is what earns the `Store` protocol: it is written against two real,
genuinely different implementations rather than one implementation and an aspiration.
(004/F23 — a protocol with one implementation is a nominal boundary.)

### 7.3 The anti-sprawl rule, applied to this document

003 §8.1 stated it and 004 abandoned it. It is restated here and applied to the
proposal that abandoned it:

> **Build the general mechanism when the second case arrives, not when you can
> imagine it.**

004's `PLUGIN_FRAMEWORK.md` proposes fifteen runtime protocols, an application
compiler with a twenty-step compile pipeline, extension bundles, capability
protocols, schema fragments, payload transforms, a projections DSL, and manifest
fingerprinting — for a library that today has zero third-party extensions. That is
the 0.2 mistake at larger scale, and it is why 1.0 is specified here as *smaller than
0.3*, not larger than 0.3.

Where 004's proposals land instead:

| 004 proposal | 1.0 disposition |
|---|---|
| `Application.compile()` → `ApplicationPlan` | `App` is a frozen Pydantic model validated in its constructor. ~100 lines, no compile phase. |
| `StateNormalizer` protocol | A Pydantic model validator. |
| `CommitPolicy` protocol | A function the caller runs. The write path is a method call the caller controls. |
| `CommitObserver` protocol | An `Inline()` subscription. |
| `MetadataProvider` protocol | The caller passes `meta=`. |
| `Extension` / `Contributions` bundles | A module that exports a list of `Subscription` values. |
| `RevisionLayout` public protocol | `encoding`: a closed internal set with a conformance suite. |
| `PayloadTransform` (compression/encryption) | Not in 1.0. Deferred with no placeholder. |
| `Projection` declarations, `Indexed()` markers | Not in 1.0. `where()` over head JSON, plus operator-managed indexes. |
| `SchemaFragment`, storage manifests | `eventic schema check` reads the database. |
| Capability marker protocols | Two backends, one conformance suite, explicit `Store.capabilities`. |
| Stable subscription IDs; no import-time registration; typed metadata; sealed kernel; canonical-state discipline; delivery state machine; sync/async explicitness | **Adopted in full.** These are 004's real contributions and they are load-bearing here. |

---

## 8. What eventic is not

- **Not an ORM.** Aggregates are logs of revisions, not mutable rows.
- **Not a durable-execution engine.** It records durable *intent* to deliver. Queues,
  retries, and workflows past the outbox belong to the consumer.
- **Not an event-sourcing framework.** The log stores state per revision, not named
  intents. Deliberate, permanent scope limit (§1.1).
- **Not polymorphic.** One stream holds one concrete state type. Model a family with a
  discriminated union field, not with subclassing.
- **Not a query engine.** `where()` is equality over top-level and dotted head paths.
  A predicate AST arrives when a second predicate kind does.
- **Not async, yet.** §9.
- **Not exactly-once.** I9.

### 8.1 Explicitly out of scope for 1.0

Deletion/tombstone semantics · scalar or `RootModel` streams · cross-database atomic
commit · multi-region conflict resolution · automatic entry-point discovery · a
plugin base class · read-your-writes inside a batch (§10.3) · compression and
encryption · a projections DSL · schema inference from existing rows.

---

## 9. Async is deferred, not designed out

1.0 ships synchronous. Async is expected later and the architecture is built so that
adding it touches **no file above the store protocol**. The full rule set is
`ARCHITECTURE.md` §9; the property that makes it work is stated here because it is a
concept-level constraint, not an implementation detail:

> **Every public operation is exactly one round trip.**

Function color propagates transitively upward from I/O. If orchestration never
interleaves logic with I/O, orchestration never acquires a color and is shared
verbatim between the sync and async runtimes. The async port is then a new file per
item *below* the protocol line — roughly 350 lines — and an edit to nothing above it.

Every rule that buys this is independently forced by a review finding: keeping
SQLAlchemy `Session` out of protocol signatures is 004/F07 and 004/F23; returning
pages instead of lazy iterators is 004/F29; rejecting `async def` handlers at
declaration time is 004/F19; banning `ContextVar` is 004/F16. **Async-readiness is a
free byproduct of building 1.0 correctly.**

---

## 10. Decisions, with the reasoning

### 10.1 State is the user's model; eventic owns an envelope

`Revision[T, M]` is a generic Pydantic model wrapping the user's plain `T`. This one
move deletes `Record`, `Draft`, `MANAGED`, `__init_subclass__`, the mixin-versus-
keyword debate, and the entire class of findings where managed fields were client
input (004/F02) or computed fields poisoned durable state (004/F06). It also produces
useful OpenAPI schemas with no FastAPI-specific code, because `Revision[Todo]` is
just a Pydantic generic.

`Draft` disappears without replacement: the user's state model is an ordinary,
mutable Pydantic model, so `s = rev.state.model_copy(deep=True); s.text = "…"` is
what a Python programmer already knows, and `col.replace(rev, s)` commits it.

### 10.2 Computed fields are never persisted

A computed field is derived state. Persisting derived state in an append-only log
contradicts I3. Canonicalization strips computed fields at every level of the model
graph and then *verifies* the result round-trips. The verification is the real
guarantee: any gap in the static strip becomes a loud error at write time instead of
an undecodable row discovered months later. (004/F06.)

### 10.3 Compare-and-swap replaces read-your-writes

`runtime.batch()` accumulates writes purely and issues one `store.commit(batch)`. It
offers no reads — so there is no read-your-writes question to answer inconsistently
(004/F17). Read-then-write logic reads first, computes, and commits with
`expected_revision`; a conflict is loud and retryable. That is the optimistic pattern,
it scales better than a held transaction, and it keeps the store protocol at one round
trip per operation (§9).

A long-lived interactive transaction is addable later as a strict superset. It is not
needed to make 1.0 correct, and adding it now would color the entire runtime.

### 10.4 The digest, not the JSONB, is the content identity

Postgres `JSONB` does not preserve key order and normalizes numbers, so "byte-
identical replay" cannot be checked by reading the column back. Every log row
therefore stores `digest = sha256(canonical bytes of the logical document)`. Replay
comparison (I7), head-rebuild verification (I2), and `eventic verify` all compare
digests. For a delta-encoded row the stored payload is the physical delta while the
digest is still that of the *reconstructed logical document* — which is precisely
what makes exactness checkable at any time.

### 10.5 Encoding is physical, chosen at the store, closed

Delta storage has produced a correctness bug in every version: ghost fields on
removal (003/F4), full-history point reads (003/F17), unreconstructable streams and
corrupted rebuilds (004/F02, 004/F11). The logical state of every revision is always
a complete canonical document; `snapshot/1` and `delta/1` are physical strategies
selected in deployment configuration (`Postgres(url, encodings={todos: Delta(every=20)})`),
never in the `Stream` declaration, and never a public protocol. Both pass the same
encoding conformance suite, whose central assertion is byte-exact reconstruction
against the digest.

### 10.6 Two backends, no `MemoryStore`

004 proposes Postgres-only with an in-memory reference store. Inverted here: SQLite
*is* the memory store, it is the only one that can honestly exercise transactionality
and constraint enforcement, and having two real SQL backends is what makes the
`Store` protocol earned rather than nominal. Both run the same conformance suite;
dialect divergence (004/F04, 004/F21) is caught by running the suite, not by hoping.
SQLite is supported for development, testing, and single-process deployments, with
its narrower capabilities declared rather than implied.

### 10.7 Declarations are values; the CLI loads them by import path

No decorators, no import-time registration, no entry-point scanning. `App` is a
frozen value; a worker is `eventic --app myapp:app worker --queue search`. Every
durable declaration — stream name, subscription id — is a stable string the user
chose, so refactoring a Python function never strands an outbox row (004/F26).

### 10.8 Metadata is typed and versioned, like state

`meta` is not a `dict[str, Any]` side channel. An application declares
`App(meta=Meta(RequestMeta, version=1))`, and metadata gets the same canonicalization,
schema version, and upcaster chain as stream state. One mechanism, applied twice.
Applications that need none use the empty `NoMeta` default, and `Revision[Todo]`
remains the natural spelling.

---

## 11. The public shape

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

t = ev[todos].create(Todo(text="learn eventic"))   # Revision[Todo], revision 0
t = ev[todos].change(t, done=True)                 # CAS on t.revision → revision 1

ev[todos].get(t.id)                                # latest, from the head
ev[todos].get(t.id, revision=0)                    # exact, from the log
ev[todos].history(t.id)                            # Page[Revision[Todo]]
ev[todos].where(done=True)                         # Page[Revision[Todo]]

with ev.batch() as b:                              # one transaction, one commit
    b[todos].change(t, done=True)
    b[audits].create(Audit(action="todo.completed"))
```

`ev[todos]` returns `Collection[Todo]` — full static typing derived from `Stream[T]`,
no casts, no registry lookup, no plugin resolution.

---

## 12. Definition of done

1.0 is finished when all of these are true. Each is a test, not a judgement.

1. No eventic class appears in any user model's MRO.
2. Managed identity and commit metadata cannot be supplied as state input.
3. A stale, fabricated, or unsaved revision cannot create a gap — it raises.
4. Two streams may safely use the same aggregate UUID.
5. Heads are byte-exactly rebuildable from the log; rebuilding changes no digest and
   leaves no orphan.
6. A commit writes log row, head row, and every intent, or writes none of them.
7. Pydantic computed fields never enter durable state, at any nesting depth.
8. Every historical row declares schema version and encoding sufficient to decode it.
9. `import eventic` imports pydantic and nothing else; `eventic.sql` imports SQLAlchemy.
10. SQLite and PostgreSQL pass one identical store conformance suite.
11. `snapshot/1` and `delta/1` pass one identical encoding conformance suite, whose
    assertion is digest equality.
12. The installed wheel contains migrations, `py.typed`, and the CLI; a fresh process
    can load an app, migrate, write, drain, and read back.
13. No credential or secret appears in a log row, an intent, an error, or a log line.
14. The async-readiness rules (`ARCHITECTURE.md` §9) are enforced by an automated test,
    not by discipline.
15. The word "exactly once" appears nowhere in the documentation.
