# Eventic 1.0 — Architecture

**Companion to** `CONCEPT.md` (the idea and the invariants) and
`IMPLEMENTATION_GUIDE.md` (the ordered route). This document fixes the module graph,
the public types, the store contract, the physical schema, and the rules that keep
async a later additive change.

Invariant references (`I1`–`I10`) are defined in `CONCEPT.md` §4. Finding references
(`003/F4`, `004/F01`) are to the two prior review documents.

---

## 1. Module graph

```text
src/eventic/
├── __init__.py            # public re-exports; imports pydantic only
├── py.typed
│
│   ── leaves (no eventic imports) ─────────────────────────────
├── errors.py              # the full exception tree
├── ids.py                 # StreamName, AggregateKey, revision_id()
├── jsonx.py               # JsonValue, canonical_bytes(), digest()
│
│   ── pure core ───────────────────────────────────────────────
├── canonical.py           # computed-field strip, canonicalize, verify round trip
├── evolution.py           # Upcaster, chain validation, upcast()
├── stream.py              # Stream[T]
├── meta.py                # Meta[M], NoMeta
├── envelopes.py           # Revision[T,M], Commit[T,M], Page[X], Cursor
├── subscription.py        # Subscription, Inline, Outbox, Backoff
├── app.py                 # App  (frozen, self-validating)
├── wire.py                # CommitRequest/Result, StoredRevision, Intent, Settlement
├── planning.py            # build requests, changed-field diff, expected_revision
├── hydration.py           # StoredRevision -> Revision[T,M]
├── retry.py               # attempt + error -> Disposition
│
│   ── the I/O boundary ────────────────────────────────────────
├── protocols.py           # Store (7 methods), StoreAdmin, Capabilities
│
│   ── orchestration (pure delegation) ─────────────────────────
├── runtime.py             # Runtime, Collection[T], Batch
├── dispatch.py            # inline dispatch, InlineDispatchError
│
│   ── closed encodings ────────────────────────────────────────
├── encodings/
│   ├── __init__.py        # ENCODINGS registry (closed set, keyed by wire id)
│   ├── snapshot.py        # snapshot/1
│   └── delta.py           # delta/1
│
│   ── the SQL backend ─────────────────────────────────────────
├── sql/
│   ├── __init__.py        # SQLite, Postgres  (imports SQLAlchemy)
│   ├── tables.py          # SQLAlchemy Core Table objects — the single schema source
│   ├── statements.py      # PURE statement builders (no execution)
│   ├── dialect.py         # per-dialect JSON access, upsert, locking, capabilities
│   ├── store.py           # Store impl: execute glue only
│   ├── admin.py           # StoreAdmin impl: migrate, check, rebuild, verify
│   └── migrations/        # alembic.ini fragment, env.py, script.py.mako, versions/
│
│   ── delivery ────────────────────────────────────────────────
├── worker.py              # outbox worker loop
│
│   ── operator surface ────────────────────────────────────────
├── cli/
│   ├── main.py            # argument parsing, exit codes
│   ├── loader.py          # "module:attr" -> App
│   └── commands/          # schema, heads, verify, worker, intents
│
└── testing/
    ├── conformance/
    │   ├── store.py       # store scenarios, declarative
    │   ├── encoding.py    # encoding scenarios, declarative
    │   └── delivery.py    # delivery scenarios, declarative
    ├── runner.py          # sync scenario runner (the async twin is one file)
    └── factories.py       # type-zoo models, deterministic builders
```

### 1.1 Dependency rules (enforced by a test, not by convention)

| Rule | Rationale |
|---|---|
| `errors`, `ids`, `jsonx` import nothing from eventic. | Leaves. |
| `protocols` imports only `wire`, `ids`, `errors`, and typing. **No SQLAlchemy, ever.** | Otherwise the protocol is welded to sync SQLAlchemy forever (§9 R5; 004/F07, 004/F23). |
| `sql/*` imports `protocols` and pure core; never `runtime`, never `cli`. | A backend must be usable without the runtime, which is how conformance tests run. |
| `runtime` imports `protocols` and pure core; never `sql`. | The runtime must not know a backend exists. |
| `cli` imports everything; nothing imports `cli`. | Operator surface is a leaf consumer. |
| `eventic/__init__.py` imports **pydantic only**. `eventic.sql` is the first module that imports SQLAlchemy. | `import eventic` stays instant and dependency-honest; verified in a fresh interpreter. |
| No module-level mutable state anywhere. | I4/I5. A test asserts the package defines zero module-level `dict`/`list`/`set` that is not `Final` and immutable. |

---

## 2. Public types

### 2.1 Declarations

```python
class Stream[T: BaseModel]:
    model: type[T]
    name: str                                   # durable identity; [a-z0-9_.-]{1,64}
    schema_version: int = 1                     # >= 1
    upcasters: Mapping[int, Upcaster] = {}      # n -> (n+1)
    # derived, cached at construction:
    #   adapter       : TypeAdapter[T]
    #   exclude_map   : nested exclude spec for computed fields
    #   fingerprint   : sha256 of the model's JSON schema
```

`Stream` is immutable and hashable by `name`. Construction validates: name shape,
`schema_version >= 1`, a complete upcaster chain `1 → schema_version`, and that the
model is a `BaseModel` subclass (not a `RootModel`, not a scalar — `CONCEPT.md` §8.1).

```python
class Meta[M: BaseModel]:
    model: type[M]
    version: int = 1
    upcasters: Mapping[int, Upcaster] = {}

NoMeta: Meta[_Empty]        # the default; serializes to {}
```

```python
class Subscription[T, M]:
    id: str                                     # durable identity, user-chosen
    stream: Stream[T]
    handler: Callable[[Commit[T, M]], None]
    kinds: frozenset[Kind] = {"create", "change"}
    delivery: Inline | Outbox = Inline()

class Inline:            pass
class Outbox:
    queue: str = "default"
    retry: Backoff = Backoff(max_attempts=12, base=1.0, factor=2.0, cap=3600.0)
    dead_letter: bool = True
```

```python
class App:
    id: str
    streams: tuple[Stream, ...]
    meta: Meta = NoMeta
    subscriptions: tuple[Subscription, ...] = ()
    on_inline_error: Literal["raise", "log"] = "raise"

    def bind(self, store: Store) -> Runtime: ...
```

`App.__init__` validates and raises `ConfigError` subclasses on:

| Check | Error |
|---|---|
| duplicate stream name or subscription id | `DuplicateId` |
| a subscription referencing a stream not in `streams` | `UnknownStream` |
| `inspect.iscoroutinefunction(handler)` | `UnsupportedHandler` — *"async handlers are not supported in 1.0"* (004/F19) |
| handler arity/annotation mismatch | `UnsupportedHandler` |
| incomplete upcaster chain on any stream or on meta | `IncompleteUpcasterChain` |
| an `Outbox` subscription when the bound store lacks the capability (checked in `bind`) | `CapabilityUnsupported` |

All checks run and are reported together, one line per failure. No compile phase, no
plan object: `App` is a frozen Pydantic model whose constructor is the validator.

### 2.2 Envelopes

```python
class Revision[T: BaseModel, M: BaseModel](BaseModel):
    stream: str
    id: UUID
    revision: int
    revision_id: UUID
    state: T
    meta: M
    committed_at: datetime          # UTC, assigned by the database
    digest: str                     # sha256 hex of the canonical document

class Commit[T, M](BaseModel):
    kind: Literal["create", "change"]
    revision: Revision[T, M]
    changed: frozenset[str]         # top-level keys whose canonical value differs
                                    # (all keys on create)

class Page[X](BaseModel):
    items: tuple[X, ...]
    cursor: str | None              # opaque; None means exhausted
```

`changed` is derived from canonical before/after documents, so an inline handler and
a worker rebuilding the same commit from the log receive field-for-field identical
envelopes (004/F09, 004/F10). It is JSON-native by construction; caller kwargs never
reach a durable row.

`Page` — never a generator (§9 R2). `history` and `where` return pages with cursors.

### 2.3 Runtime surface

```python
class Runtime:
    def __getitem__[T](self, stream: Stream[T]) -> Collection[T]: ...
    def batch(self) -> Batch: ...                     # context manager
    def admin(self) -> StoreAdmin: ...

class Collection[T]:
    def create(self, state: T, *, id: UUID | None = None,
               meta: M | None = None) -> Revision[T, M]: ...
    def change(self, base: Revision[T, M], /, **fields) -> Revision[T, M]: ...
    def replace(self, base: Revision[T, M], state: T, *,
                meta: M | None = None) -> Revision[T, M]: ...
    def get(self, id: UUID, *, revision: int | None = None) -> Revision[T, M]: ...
    def history(self, id: UUID, *, after: int = -1,
                limit: int = 100) -> Page[Revision[T, M]]: ...
    def where(self, *, limit: int = 100, cursor: str | None = None,
              **filters) -> Page[Revision[T, M]]: ...

class Batch:
    def __getitem__[T](self, stream: Stream[T]) -> BatchCollection[T]: ...
    # BatchCollection exposes create / change / replace ONLY.
    # There is no read on a Batch — see CONCEPT.md §10.3.
```

`change` validates `base.state.model_dump() | fields` through the stream adapter — it
never uses `model_copy(update=...)`, which bypasses validation (004/F08).
`Batch.__exit__` issues exactly one `store.commit(requests)` and then dispatches
inline handlers for every result in request order.

---

## 3. Canonicalization

The single most load-bearing pure module. `canonical.py` exposes three functions:

```python
def canonicalize(stream_or_meta, value) -> bytes
def verify(stream_or_meta, payload: bytes) -> None        # raises UndecodableRevision
def digest(payload: bytes) -> str                          # sha256 hex
```

### 3.1 The algorithm

1. **Strip computed fields.** At `Stream` construction, walk the annotated model graph
   and build a nested `exclude` specification covering every `model_computed_fields`
   entry at every depth, using Pydantic's nested-`exclude` and `__all__` forms for
   sequences and mappings. Cache it on the stream.
2. **Dump.** `adapter.dump_python(value, mode="json", exclude=exclude_map, by_alias=False)`.
3. **Canonicalize.** `json.dumps(tree, sort_keys=True, separators=(",", ":"),
   ensure_ascii=False, allow_nan=False).encode("utf-8")`.
   Sorted keys — never Pydantic's field order, which changes when a developer reorders
   source lines and would silently change every future digest.
4. **Verify the round trip.** `adapter.validate_json(payload)`, re-run steps 1–3 on the
   result, and require byte equality. Failure raises `UndecodableRevision` **before the
   row is written**.
5. **Digest.** `sha256(payload).hexdigest()`.

Step 4 is the guarantee, and step 1 is the optimization. Unions, `Any`, and arbitrary
nesting can defeat a static walk; they cannot defeat the round trip. Any gap becomes a
loud write-time error rather than an undecodable row found months later (004/F06).

Verification runs on every write. It is two serializations and a comparison on data
already in memory; if profiling ever shows it matters, it becomes
`Store(..., verify="always" | "sampled")` — never silently off.

### 3.2 Type zoo

Canonicalization is proven against a fixed corpus, not against examples: `UUID`,
`datetime`/`date`/`time` (aware and naive), `Decimal`, `Enum`, `IntEnum`, `bytes`,
`SecretStr`, `Path`, nested models, discriminated unions, `list`/`dict`/`tuple`/`set`,
`Optional`, defaults, aliases, field and model serializers, field and model
validators, computed fields at depth 0/1/2, and models inside sequences and mappings.
The property for every member: `canonicalize(validate(canonicalize(x))) == canonicalize(x)`.

`SecretStr` is a deliberate trap in the corpus: it must either round-trip losslessly
or be rejected at `Stream` construction. It must never serialize as `"**********"`
into an append-only log.

---

## 4. The store contract

### 4.1 Wire types

```python
@dataclass(frozen=True, slots=True)
class CommitRequest:
    stream: str
    aggregate_id: UUID
    expected_revision: int | None     # None => the aggregate must not exist
    kind: Literal["create", "change"]
    schema_version: int
    payload: bytes                    # canonical document
    digest: str
    meta: bytes
    meta_version: int
    intents: tuple[IntentRequest, ...]

@dataclass(frozen=True, slots=True)
class CommitResult:
    stream: str
    aggregate_id: UUID
    revision: int
    revision_id: UUID
    committed_at: datetime
    replayed: bool                    # True => identical row already existed

@dataclass(frozen=True, slots=True)
class StoredRevision:
    stream: str
    aggregate_id: UUID
    revision: int
    revision_id: UUID
    kind: str
    schema_version: int
    meta_version: int
    encoding: str
    payload: JsonValue                # LOGICAL document, already decoded by the store
    digest: str
    meta: JsonValue
    committed_at: datetime
```

`StoredRevision.payload` is always the logical document. Physical encoding never
escapes the store — that is what keeps `hydration.py` encoding-agnostic and what makes
a rebuild reproduce the head exactly (004/F11).

### 4.2 The protocol

```python
class Store(Protocol):
    @property
    def capabilities(self) -> Capabilities: ...

    def commit(self, requests: Sequence[CommitRequest]) -> Sequence[CommitResult]: ...
    def head(self, key: AggregateKey) -> StoredRevision | None: ...
    def revision(self, key: AggregateKey, revision: int) -> StoredRevision | None: ...
    def history(self, key: AggregateKey, *, after: int, limit: int) -> Page[StoredRevision]: ...
    def search(self, stream: str, filters: Mapping[str, JsonValue], *,
               cursor: str | None, limit: int) -> Page[StoredRevision]: ...
    def claim(self, queue: str, *, limit: int, lease: timedelta) -> Sequence[ClaimedIntent]: ...
    def settle(self, settlements: Sequence[Settlement]) -> None: ...
```

Seven methods. Every one is *one request in, one value out*: no callbacks, no
generators, no session arguments, no open transactions handed back to the caller.
That shape is what §9 requires and what makes the conformance suite runnable against
any backend.

```python
class StoreAdmin(Protocol):          # sync forever — CLI only, never in a hot path
    def migrate(self) -> None: ...
    def check(self, app: App) -> SchemaReport: ...
    def rebuild_heads(self, stream: str | None, *, chunk: int) -> RebuildReport: ...
    def verify(self, stream: str | None, *, chunk: int) -> VerifyReport: ...
```

```python
@dataclass(frozen=True)
class Capabilities:
    outbox: bool                  # transactional delivery intents
    json_paths: bool              # dotted-path equality pushdown in search()
    concurrent_drainers: bool     # row-level claim locking (Postgres yes, SQLite no)
    max_batch: int
```

Capabilities describe behavior the conformance suite tests, not marker attributes
(004/F23). `App.bind` checks them once: an `Outbox` subscription against a store with
`outbox=False` raises `CapabilityUnsupported` at bind time, not at first write.

### 4.3 `commit` — the required algorithm

Inside one transaction, for each request in order:

1. **CAS.** Read the head row for `(stream, aggregate_id)` with row-level locking.
   - `expected_revision is None` and a head exists → `RevisionConflict`.
   - `expected_revision is not None` and head is absent, or `head.revision != expected_revision` → `RevisionConflict`.
   - Constraint violation on `(stream, aggregate_id, revision)` also maps to
     `RevisionConflict`. The unique index is the backstop, the CAS is the diagnosis.
2. **Replay check.** If a row already exists at `(stream, aggregate_id, revision)`:
   return `replayed=True` **only if** `digest`, `kind`, `schema_version`,
   `meta_version`, and canonical meta all match. Any other difference is
   `RevisionConflict`. Cross-stream identical UUIDs are structurally impossible because
   the key includes the stream (004/F03).
3. **Encode.** Apply the configured encoding for this stream to produce the physical
   payload (§5).
4. **Insert the log row.**
5. **Derive the head by decoding what was just encoded** and upserting it. Not from the
   request payload — from the encoded row. Then assert the decoded digest equals
   `request.digest`; a mismatch aborts the transaction with `EncodingError`. This is I3
   made mechanical (004/F01).
6. **Insert delivery intents.**
7. Once per process per `(stream, schema_version)`, upsert the fingerprint row (§6.4).

Then `COMMIT`, and read back `committed_at` for every result. Timestamps come from the
database clock (`now()` / `CURRENT_TIMESTAMP`), never from the application process.

Requests in a batch are applied in order and may touch the same aggregate more than
once; each subsequent request's `expected_revision` must chain. All succeed or none do.

### 4.4 Errors from a store

A backend raises only from the public tree (§8). Driver exceptions are translated at
the store boundary; a `psycopg` or `sqlite3` exception must never reach a caller.

---

## 5. Encodings

Closed set, keyed by a durable wire id stored on every row.

### 5.1 `snapshot/1`

`payload` is the canonical document verbatim. `window() == 1`. Reconstruction is the
identity function. This is the default for every stream.

### 5.2 `delta/1`

```json
{"base": 40, "set": {"text": "…"}, "del": ["tag"]}
```

Top-level keys only, with explicit tombstones — the absence of tombstones is exactly
003/F4, where removed fields resurrected on read. A checkpoint (a full `snapshot/1`
row) is written every `every=K` revisions and always at revision 0.

Reconstruction of revision *n* reads the bounded window `[checkpoint(n) … n]` — one
range query, at most `K` rows, never the whole history (003/F17). The store rejects a
window with a missing checkpoint or a broken `base` chain as `UndecodableRevision`
rather than returning a partial document.

### 5.3 The conformance assertion

For any stream, any encoding, and any sequence of writes:

```
decode(rows[0 … n]).digest == log[n].digest        for every n
```

Digest equality, not object equality. That is what makes I2 checkable by `eventic
verify` on a live database at any time, and it is why the digest column exists
(`CONCEPT.md` §10.4).

Selection is deployment configuration, never a `Stream` declaration:

```python
Postgres(url, encodings={todos: Delta(every=20)})     # default: Snapshot()
```

---

## 6. Physical schema

One source of truth: `sql/tables.py` (SQLAlchemy Core `Table` objects). Alembic
revisions are generated from it and `alembic check` is a CI gate — 004/F04 was a
hand-written migration drifting from the ORM, which is a process failure, not a typo.

All dialect-varying types are declared with `.with_variant(...)` in `tables.py` so the
variant cannot be forgotten in a migration.

### 6.1 `eventic_revision` — the log (I1)

| column | type | notes |
|---|---|---|
| `revision_id` | UUID | PK; `uuid5(NS, "{stream}:{id}:{revision}")` (I6) |
| `stream` | TEXT NOT NULL | |
| `aggregate_id` | UUID NOT NULL | |
| `revision` | INT NOT NULL | `CHECK (revision >= 0)` |
| `kind` | TEXT NOT NULL | `CHECK (kind IN ('create','change'))` |
| `schema_version` | INT NOT NULL | `CHECK (schema_version >= 1)` (I10) |
| `meta_version` | INT NOT NULL | |
| `encoding` | TEXT NOT NULL | `'snapshot/1'` \| `'delta/1'` (I10) |
| `payload` | JSONB / JSON | physical, per `encoding` |
| `digest` | CHAR(64) NOT NULL | sha256 of the **logical** document |
| `meta` | JSONB / JSON NOT NULL | canonical |
| `committed_at` | TIMESTAMPTZ NOT NULL | database clock |

- `UNIQUE (stream, aggregate_id, revision)` — the aggregate key is `(stream, id)` (I6)
- `CHECK ((revision = 0) = (kind = 'create'))`
- `CHECK (stream <> '')`
- `INDEX (stream, aggregate_id, revision)` — serves point, window, and history reads
- No other index on this table. 003/F18 was three overlapping indexes on one prefix.
- **Production guidance:** grant the application role `INSERT` and `SELECT` only. I1
  should survive direct database access.

### 6.2 `eventic_head` — the projection (I2)

| column | type | notes |
|---|---|---|
| `stream`, `aggregate_id` | | composite PK |
| `revision`, `revision_id` | | |
| `schema_version`, `meta_version` | | |
| `state` | JSONB / JSON NOT NULL | the **logical** canonical document |
| `digest` | CHAR(64) NOT NULL | equal to the log row's digest |
| `meta` | JSONB / JSON NOT NULL | |
| `committed_at` | TIMESTAMPTZ NOT NULL | |

- `INDEX GIN (state) jsonb_path_ops` on Postgres; documented expression indexes on SQLite
- Fully derived. `eventic heads rebuild` truncates the selected scope inside the
  transaction and rebuilds it, so orphans cannot survive (004/F11), then compares
  digests against the log.

### 6.3 `eventic_intent` — the outbox

| column | type | notes |
|---|---|---|
| `intent_id` | UUID | PK |
| `subscription_id` | TEXT NOT NULL | user-chosen, stable (004/F26) |
| `revision_id` | UUID NOT NULL | the commit being delivered |
| `queue` | TEXT NOT NULL | `CHECK (queue <> '')` |
| `status` | TEXT NOT NULL | `CHECK (status IN ('pending','leased','dead'))` |
| `attempts` | INT NOT NULL DEFAULT 0 | |
| `available_at` | TIMESTAMPTZ NOT NULL | |
| `leased_until` | TIMESTAMPTZ NULL | |
| `last_error` | TEXT NULL | redacted, truncated to 2 KiB |
| `created_at` | TIMESTAMPTZ NOT NULL | |

- `UNIQUE (subscription_id, revision_id)` — one function may hold several
  subscriptions without colliding, because identity is the subscription, not the
  function (004/F14)
- `INDEX (queue, status, available_at)` — the drain query's exact shape (004/F25)
- Successful delivery **deletes** the row. Dead-lettering sets `status='dead'` and
  retains it for `intents list` / `redrive`.

### 6.4 `eventic_schema` — the fingerprint ledger (I10)

`(stream, schema_version)` PK, plus `fingerprint`, `first_seen`. Written once per
process per pair on first commit; `eventic schema check` compares the declared
fingerprint to the stored one and reports drift. This catches "the model changed
without a `schema_version` bump" at deploy time rather than at read time.

### 6.5 Dialect differences, declared

| | PostgreSQL | SQLite |
|---|---|---|
| JSON type | `JSONB` | `JSON` (TEXT) |
| head search | `@>` containment plus explicit path equality | `json_extract(...) = ?` |
| missing vs. JSON null | distinguished explicitly by the query builder | distinguished explicitly by the query builder |
| intent claim | `FOR UPDATE SKIP LOCKED` | `BEGIN IMMEDIATE` + single-drainer |
| `concurrent_drainers` | `True` | `False` |
| autoincrement | n/a — intents are UUID-keyed, so 004/F04 cannot recur | n/a |

004/F21 (backend-dependent equality) is closed by *specifying* the semantics —
missing path and JSON null are distinct, dotted segments are escaped — and running one
identical conformance suite on both. Where a dialect cannot express a semantic, the
capability flag says so and the suite skips by capability, never by dialect name.

---

## 7. Delivery

### 7.1 Staging

At commit time, `planning.py` computes the intents for a request purely: every
subscription whose `stream` matches and whose `kinds` contains the commit kind and
whose `delivery` is `Outbox`. They are written in the same transaction (I8). Inline
subscriptions produce no rows.

### 7.2 The state machine

```
            commit
              │
              ▼
          [pending] ──claim──► [leased] ──ack──► (deleted)
              ▲                   │
              └───nack(retry)─────┤
                                  └───attempts exhausted───► [dead]
                                                                │
                                                          redrive │
                                                                ▼
                                                            [pending]
```

Worker loop, per batch:

1. **Claim** — short transaction: select `status='pending' AND available_at <= now()`
   for the queue, mark `leased`, set `leased_until`, bump `attempts`. Returns rows.
2. **Deliver** — *outside any transaction*. Load the revision, upcast, hydrate,
   build `Commit`, call the handler. No database lock is held while user code runs
   (004/F25), which is also what makes the loop portable to async unchanged.
3. **Settle** — short transaction: delete on success; on failure compute the
   disposition purely (`retry.py`: `Disposition.retry(available_at)` or
   `Disposition.dead(reason)`) and apply it.

Expired leases return to `pending` implicitly: the claim query treats
`status='leased' AND leased_until < now()` as claimable.

### 7.3 Contract

At-least-once. A side effect may succeed and the ack may fail, so handlers must be
idempotent, and the documentation says so in those words (I9, 004/F20). The worker
returns structured counts — `claimed`, `delivered`, `retried`, `dead_lettered` — and
the CLI exits non-zero when `dead_lettered > 0` or when the app cannot be loaded
(004/F13).

### 7.4 Inline

Runs in the writing process after `store.commit` returns, in declaration order. Every
handler runs even if an earlier one raises; failures are collected and re-raised as
`InlineDispatchError` (default) or logged (`App(on_inline_error="log")`). Never
silently swallowed. The commit has already happened and is not affected.

---

## 8. Errors

```text
EventicError
├── ConfigError                  # declaration-time
│   ├── DuplicateId
│   ├── UnknownStream
│   ├── UnsupportedHandler       # async handler in a sync runtime (004/F19)
│   └── IncompleteUpcasterChain
├── UsageError                   # API misused
├── NotFound                     # aggregate or exact revision absent
├── RevisionConflict             # CAS failed: stale, fabricated, or concurrent
├── EncodingError                # encode/decode disagreement, broken delta chain
├── UndecodableRevision          # round-trip verification or upcast failure
├── CapabilityUnsupported        # store lacks a required capability
├── StoreError                   # translated backend failure
└── DeliveryError
    ├── InlineDispatchError
    └── DeadLettered
```

`pydantic.ValidationError` is never wrapped or swallowed. `NotFound` does **not**
subclass `KeyError` — 003/F15's fix was a compatibility gesture with no remaining
audience in a fresh 1.0.

Every error carries structured attributes (`stream`, `aggregate_id`, `revision`,
`subscription_id` where relevant), and no error message ever interpolates a payload,
a credential, or a connection URL.

---

## 9. Async-readiness rules

1.0 ships sync. These ten rules make the future async port a set of new files below
the protocol line, with no edit above it. Rule 10 makes them enforced rather than
remembered.

| # | Rule | Also closes |
|---|---|---|
| **R1** | The `Store` protocol is seven methods, each *one request in, one value out*. No callbacks, no session arguments, no returned open transactions. | 004/F23 |
| **R2** | No generators or lazy iterators cross the I/O boundary. Reads return `Page`. Generator → async generator is not a mechanical port. | 004/F29 |
| **R3** | Zero I/O above `protocols.py`. `planning`, `hydration`, `canonical`, `evolution`, `retry` are pure functions over values. | 004/F01, 004/F07 |
| **R4** | SQL is data. `sql/statements.py` builds SQLAlchemy Core constructs and executes nothing; `sql/store.py` is `.execute()` glue. ~70% of the backend is shared with the future async store verbatim. | — |
| **R5** | No `Session`, `Connection`, `Engine`, or any SQLAlchemy type in a protocol or public signature. | 004/F07, 004/F23 |
| **R6** | Handler color is decided at declaration: `App` rejects coroutine functions with a forward-compatible message. | 004/F19 |
| **R7** | Two concrete protocols later (`Store`, `AsyncStore`) — never one `Awaitable[T] \| T` generic, which destroys type-checker and IDE output. | 004/F28 |
| **R8** | Conformance suites are declarative scenarios plus a thin runner. The async suite is a second runner, not a copy. | 004/F31 |
| **R9** | No `ContextVar`, no thread-local, no module-level mutable state. This is the bug that reappears worse across asyncio tasks. | 004/F16, I4/I5 |
| **R10** | `StoreAdmin` is sync forever. Migration, rebuild, and verify are CLI operations and never need an async twin. | — |

### 9.1 The enforcement test

`tests/architecture/test_async_ready.py` asserts mechanically:

- every method annotation in `protocols.py` resolves without importing `sqlalchemy`;
- no return annotation in `protocols.py` is `Iterator`, `Iterable`, or `Generator`;
- no `yield` appears in `sql/store.py`;
- no `Callable` appears in any `Store` method parameter;
- `import eventic` in a fresh interpreter leaves `sqlalchemy` out of `sys.modules`;
- the package defines no module-level mutable binding;
- the import graph matches §1.1.

### 9.2 The projected port

| Component | Port |
|---|---|
| `protocols.py` → `protocols_async.py` | signature duplication, ~60 lines |
| `sql/store.py` → `sql/async_store.py` | `.execute()` → `await .execute()`, ~150 lines; `statements.py` reused verbatim |
| `runtime.py` → `runtime_async.py` | delegation only, ~120 lines |
| `worker.py` → `worker_async.py` | the loop, ~80 lines; `retry.py` reused verbatim |
| everything else | **unchanged** |

An optional `AsyncRuntime` bridging to the sync runtime via `asyncio.to_thread` is ~80
lines with the same API shape, so user code does not change when a native store
arrives. It is not built at 1.0; the rules above are what keep it a one-file option.

---

## 10. Packaging

```toml
[project]
name = "eventic"
version = "1.0.0"
requires-python = ">=3.13"
dependencies = ["pydantic>=2.9", "sqlalchemy>=2.0.43"]

[project.optional-dependencies]
postgres = ["psycopg[binary]>=3.2"]
migrate  = ["alembic>=1.13"]

[project.scripts]
eventic = "eventic.cli.main:main"
```

- `python-dotenv` is gone (004/F30). The version has one source, read from package
  metadata.
- The wheel ships `py.typed`, `sql/migrations/**` (env, template, and every revision),
  the CLI, and `eventic/testing/**` so extension authors can run the conformance suites.
- The sdist explicitly excludes `.scratch/`, probes, and lockfiles (004/F05, 004/F32).
- A CI job installs the built wheel into an empty environment and runs: load an app,
  `eventic schema upgrade`, write a revision, drain a queue, read it back. That test is
  what proves the documented production path exists.

### 10.1 Documentation rules

Source comments explain current invariants and non-obvious mechanics only. No "Step N",
no finding IDs, no implementation diary in production docstrings (004/F32). Rationale
lives here and in `CONCEPT.md`.

---

## 11. Test architecture

| Layer | What it proves | Where |
|---|---|---|
| **Type-zoo property** | `canonicalize(validate(canonicalize(x))) == canonicalize(x)` for every supported Pydantic construct | `tests/property/` |
| **Four-way agreement** | Hypothesis stateful model: for any command sequence, `head == replay(log) == history[-1] == returned value == rebuilt head`, compared by digest | `tests/property/` |
| **Store conformance** | The §4 contract, as declarative scenarios | `eventic/testing/conformance/store.py`, run against SQLite and Postgres |
| **Encoding conformance** | §5.3 digest equality across long histories, checkpoint boundaries, missing bases, corruption | `eventic/testing/conformance/encoding.py` |
| **Delivery conformance** | Claim/lease/ack, crash after claim, crash after side effect, concurrent drainers, retry exhaustion, dead-letter, redrive | `eventic/testing/conformance/delivery.py` |
| **Architecture** | §1.1 import graph and §9.1 async-readiness | `tests/architecture/` |
| **Typing** | `basedpyright` over the README examples as fixtures | `tests/typing/` |
| **Installed wheel** | §10 smoke path in a clean environment | `tests/integration/wheel/` |

The four-way agreement property is the highest-leverage artifact in the suite: it makes
004's F01, F02, F09, F10, and F11 *impossible* rather than individually tested. 004's
own review observed that the 0.3 suite "overfits the prior review" with F-numbered
checkpoint tests; **no test in 1.0 is named after a finding.**
