# Eventic Rewrite — Complete Implementation Guide

Date: 2026-08-05
Status: Proposed execution plan for a ground-up, compatibility-breaking rewrite
Companion documents: `REVIEW.md`, `PLUGIN_FRAMEWORK.md`

## 1. Purpose and how to use this guide

This document turns the proposed Pydantic-first architecture into an ordered build
program. It is intended to be followed from top to bottom. Each phase has:

- A concrete outcome.
- Exact implementation tasks.
- Suggested files and public types.
- Tests that must be written with the implementation.
- An exit gate that must pass before the next phase begins.

Do not implement later convenience APIs around an unstable core. The first objective
is a small vertical slice that proves the central invariant:

> Every successful command validates one Pydantic document, seals one canonical
> payload, atomically appends one revision, advances one head, stages all durable
> delivery intents, and returns envelopes reconstructed from that same payload.

The guide assumes a feature branch dedicated to the rewrite. The old implementation
remains recoverable in Git history; it should not remain in the runtime as a
compatibility subsystem.

## 2. Decisions fixed before implementation

These decisions are deliberately settled here. Changing one requires an explicit
architecture decision record and corresponding updates to this guide.

### 2.1 Product and compatibility

- The rewrite becomes Eventic 1.0.
- There is no compatibility shim for `Record`, `connect`, class-keyword seams,
  `save()`, the existing `Delta`, or decorator-based global subscriptions.
- Every `Application` has an explicit stable ID. Command-idempotency keys are scoped
  by that application ID inside a store.
- Existing pre-1.0 data is not read transparently. If migration is later required,
  build a separate offline export/import tool rather than contaminating the runtime.
- PostgreSQL is the only production database in 1.0.
- `MemoryStore` is the reference implementation for tests and local examples.
- SQLite is unsupported until it passes the entire store conformance suite without
  dialect-specific semantic exceptions.

### 2.2 State and identity

- A stream contains one concrete `pydantic.BaseModel` state type in 1.0.
- Eventic uses Pydantic's public `TypeAdapter` API internally.
- Aggregate identity is `(stream_name, aggregate_id)`.
- The first committed revision is revision `0`.
- Revision IDs are deterministic UUIDv5 values derived from stream, aggregate ID,
  and revision number.
- Command IDs are independent, caller-supplied idempotency keys.
- When an idempotent create omits an aggregate ID, Eventic deterministically
  generates one from application ID, command ID, and operation ordinal. A create
  without a command ID uses UUIDv4. This generation rule does not replace the command
  table or conflate command identity with revision identity.
- Eventic metadata is an envelope, never a field injected into user state.
- Metadata has its own Pydantic type, schema version, fingerprint, and evolution
  chain; it is not an unversioned `dict[str, Any]` side channel.
- Pydantic values are detached and may be mutable. Eventic does not claim recursive
  Python immutability.
- Canonical bytes, not caller-owned object graphs, are durable truth.

### 2.3 Persistence and delivery

- The logical representation of every revision is a complete canonical JSON
  document.
- Physical snapshot, delta, compression, and encryption strategies live below that
  logical contract.
- One canonical document drives the revision log, head, event, and returned value.
- Database commit timestamps are authoritative UTC timestamps.
- Durable subscriptions use transactional delivery intents and promise at-least-once
  delivery.
- Inline observers/subscriptions are explicitly best effort and post-commit.
- No public ambient unit of work or `ContextVar` selects a store.
- Atomic multi-command batches are supported through an explicit store API.

### 2.4 Extensibility

- The invariant kernel is sealed.
- Runtime extensibility uses narrow protocols.
- Optional extension bundles expand into typed declarations during compilation.
- There is no automatic entry-point discovery or import-time registration.
- Component order is declaration order; there are no priority integers.
- Every durable contribution has a stable explicit ID.
- Sync and async runtime surfaces are distinct.

### 2.5 Scope exclusions for 1.0

- No deletion/tombstone semantics.
- No arbitrary scalar or `RootModel` streams.
- No transparent multi-region conflict resolution.
- No cross-database atomic commit.
- No exactly-once side-effect claim.
- No arbitrary query AST.
- No automatic schema inference from existing database rows.

## 3. Definition of success

The rewrite is complete only when all of these statements are true:

1. User state classes inherit only from Pydantic classes chosen by the user.
2. Managed identity and commit metadata cannot be supplied as state input.
3. A stale, fabricated, or unsaved revision cannot be used to create a revision gap.
4. Two streams may safely use the same aggregate UUID.
5. Head state is exactly rebuildable from the revision log.
6. Rebuilding heads does not change any observable value or canonical digest.
7. A commit either writes revision, head, command result, and all delivery intents or
   writes none of them.
8. Idempotent replay returns the original committed result; a reused command ID with
   different input fails loudly.
9. Pydantic computed fields never enter durable state.
10. Custom serializers cannot silently change durable semantics.
11. Every historical row declares schema, layout, and transform versions sufficient
    to decode it.
12. The installed wheel contains migrations, `py.typed`, CLI code, and conformance
    helpers intended for extension authors.
13. A fresh installed-wheel process can load an application, migrate a database,
    write a revision, drain an outbox, and read the revision back.
14. PostgreSQL and memory implementations pass the same behavioral contract tests.
15. Sync and async implementations pass equivalent contract suites.
16. The CLI reports partial failures and retained/dead-lettered work truthfully.
17. No credential or secret appears in a revision, delivery intent, application
    manifest, log message, or error rendering.

## 4. Target package structure

Build toward this structure. Exact private filenames may change, but dependency
direction must remain intact.

```text
src/eventic/
├── __init__.py
├── py.typed
├── application.py          # Application declaration
├── commands.py             # Create, Change, Replace, CommitBatch
├── envelopes.py            # Revision, Committed, Proposal, typed metadata
├── errors.py               # stable public errors
├── identifiers.py          # StreamName, CommandId, deterministic IDs
├── stream.py               # Stream[T], event sources, edit helpers
├── evolution.py            # upcaster declarations and chains
├── extensions.py           # Contributions and Extension protocol
├── policies.py             # policy/normalizer/provider protocols
├── projections.py          # projection declarations and Annotated markers
├── canonical.py            # Pydantic normalization and canonical JSON
├── compiler.py             # Application -> ApplicationPlan
├── plan.py                 # immutable compiled data
├── protocols/
│   ├── store.py            # sync store/transaction contracts
│   ├── async_store.py      # async store/transaction contracts
│   ├── delivery.py
│   ├── layout.py
│   ├── projection.py
│   └── resources.py
├── runtime/
│   ├── commit.py           # sync invariant-preserving orchestration
│   ├── async_commit.py
│   ├── collection.py       # Collection[T]
│   ├── async_collection.py
│   ├── runtime.py
│   └── async_runtime.py
├── stores/
│   ├── memory.py
│   ├── async_memory.py
│   └── postgres/
│       ├── schema.py       # SQLAlchemy Core tables
│       ├── statements.py   # shared SQL builders
│       ├── store.py
│       ├── async_store.py
│       ├── transaction.py
│       ├── layouts.py
│       └── migrations/
│           ├── env.py
│           ├── script.py.mako
│           └── versions/
├── delivery/
│   ├── models.py
│   ├── inline.py
│   ├── outbox.py
│   ├── retry.py
│   ├── worker.py
│   └── async_worker.py
├── layouts/
│   ├── snapshots.py
│   ├── deltas.py
│   └── transforms.py
├── cli/
│   ├── main.py
│   ├── loader.py
│   ├── schema.py
│   ├── migrate.py
│   ├── worker.py
│   └── deliveries.py
└── testing/
    ├── stores.py
    ├── layouts.py
    ├── delivery.py
    ├── transforms.py
    └── extensions.py
```

Tests should mirror public behavior rather than private modules:

```text
tests/
├── unit/
├── contracts/
├── integration/postgres/
├── integration/installed_wheel/
├── property/
├── typing/
└── fixtures/evolution/
```

Dependency rules:

- `identifiers`, `errors`, and JSON value types are leaves.
- Protocol modules depend only on public value objects and typing.
- Store implementations depend on protocols, never on a concrete runtime.
- The commit runtime depends on `ApplicationPlan` and store protocols.
- Delivery workers depend on compiled subscriptions and store delivery protocols.
- CLI modules construct public objects; core never imports CLI.
- Core never imports a third-party extension.

Enforce the graph with an import-linter test or an equivalent static dependency
test.

## 5. Target public API before code

Write executable examples and type-checking fixtures for the intended API before
implementing it. These examples are the first contract tests.

### 5.1 Declaration

```python
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from eventic import Application, Indexed, Stream


class Todo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Annotated[str, Field(min_length=1)]
    done: bool = False
    status: Annotated[
        Literal["backlog", "active", "complete"],
        Indexed(),
    ] = "backlog"

    @computed_field
    @property
    def display_text(self) -> str:
        return f"✓ {self.text}" if self.done else self.text


Todos = Stream(Todo, name="todos", schema_version=1)
application = Application(id="todo-service", streams=[Todos])
```

### 5.2 Sync use

```python
plan = application.compile(store=PostgresStore(DATABASE_URL), runtime="sync")
runtime = Runtime(plan)
todos = runtime.collection(Todos)

todo = todos.create(Todo(text="Learn Eventic"))
todo = todos.change(todo, done=True, status="complete")

current = todos.get(todo.id)
original = todos.get(todo.id, revision=0)
history = list(todos.history(todo.id))
```

### 5.3 Detached editing

```python
draft = todo.edit()
draft.text = "Ship Eventic"
draft.status = "active"

todo = todos.replace(todo, draft)
```

`edit()` returns a freshly hydrated `Todo`. Mutating it changes no `Revision`, store,
head, or history until `replace()` succeeds.

### 5.4 Idempotency

```python
todo = todos.create(
    Todo(text="Handle payment event"),
    command_id="stripe:event:evt_123",
)

same_todo = todos.create(
    Todo(text="Handle payment event"),
    command_id="stripe:event:evt_123",
)

assert same_todo == todo
```

Changing durable command input while reusing the command ID raises
`IdempotencyConflict`.

### 5.5 Atomic batch

```python
todo, audit = runtime.commit(
    Todos.change(todo, done=True),
    AuditEntries.create(
        AuditEntry(action="todo.completed", subject_id=todo.id)
    ),
    command_id="request:01J...",
)
```

The batch preserves command order in its result and commits all operations in one
store transaction.

### 5.6 Async use

```python
plan = application.compile(store=AsyncPostgresStore(DATABASE_URL), runtime="async")
runtime = AsyncRuntime(plan)
todos = runtime.collection(Todos)

todo = await todos.create(Todo(text="Learn Eventic"))
todo = await todos.change(todo, done=True)
```

### 5.7 FastAPI use

```python
@api.post("/todos", response_model=Revision[Todo])
async def create_todo(body: Todo) -> Revision[Todo]:
    return await todos.create(body)
```

`Revision[Todo]` and event envelopes must generate useful Pydantic/OpenAPI schemas
without FastAPI-specific Eventic code.

`Revision` and `Committed` use a `NoMetadata` default type parameter, so
`Revision[Todo]` remains the natural spelling for applications that do not define a
metadata model.

## 6. Working discipline

Use these rules throughout implementation:

1. Add a failing behavioral test before implementing each invariant.
2. Keep commits scoped to one phase or coherent sub-step.
3. Never make a failing regression pass by weakening its assertion.
4. Prefer pure functions at validation, canonicalization, diff, identity, and
   upcasting boundaries.
5. Do not expose a new protocol until a real second implementation or test double
   demonstrates the need.
6. Do not mock PostgreSQL behavior in PostgreSQL integration tests.
7. Do not use `model_construct()` with persisted data.
8. Do not use `model_copy(update=...)` for validated updates.
9. Do not share SQLAlchemy sessions through module state or context variables.
10. Treat every warning in tests as an error.
11. Run type-checking fixtures as tests of developer ergonomics.
12. Do not begin performance optimization until conformance and fault tests pass.

Every phase should end with:

```console
uv run ruff check .
uv run ruff format --check .
uv run basedpyright
uv run pytest -W error
```

During early phases, target the implemented subsets explicitly; the full command
becomes mandatory at the hardening gate.

## 7. Phase 0 — Freeze the contract and preserve evidence

### Outcome

The rewrite begins from documented invariants and executable failures rather than
from the current module boundaries.

### Steps

1. Create a dedicated rewrite branch.
2. Tag or record the last pre-rewrite commit.
3. Copy the seven release-blocker scenarios from the review probes into neutral
   behavior specifications using the new vocabulary.
4. Write architecture decision records for the decisions in section 2.
5. Add the target API examples from section 5 as documentation and typing fixtures.
6. Define the public error taxonomy before code starts.
7. Define the support matrix: Python, Pydantic, SQLAlchemy, PostgreSQL, and psycopg
   minimum/latest tested versions.
8. Decide the canonical JSON implementation by evaluating maintained RFC 8785/JCS
   libraries. Do not hand-roll floating-point canonicalization.
9. Define the versioning policy for storage schema, stream schema, layouts,
   transforms, subscriptions, and the Python package.
10. Record explicit non-goals so they do not re-enter through convenience patches.

### Required tests/specifications

- A scenario table for each reviewed blocker.
- API type fixtures that show inferred `Revision[Todo]` and `Collection[Todo]` types.
- A public symbol allowlist for the intended top-level package.
- Architecture dependency rules.

### Exit gate

- Every decision in section 2 has an ADR or is copied into the authoritative concept
  document.
- Target API examples are internally consistent and type-check as stubs.
- No implementation task depends on an unresolved identity, delivery, or database
  semantic.

## 8. Phase 1 — Replace the package skeleton and establish quality gates

### Outcome

The repository builds an intentionally empty 1.0 package with strict tooling and no
legacy runtime surface.

### Steps

1. Set the version to `1.0.0a1`.
2. Replace the old `src/eventic` modules with the target skeleton. Git history is the
   compatibility archive; do not retain dead modules under `legacy`.
3. Keep only top-level imports explicitly listed in the public API contract.
4. Add `src/eventic/py.typed` and ensure the wheel includes it.
5. Move Alembic resources under `src/eventic/stores/postgres/migrations/` so the
   wheel owns its migrations.
6. Configure Hatch to include package migrations and exclude `.scratch`, tests,
   local DB files, caches, and development-only configuration from distributions.
7. Replace the broad Pydantic lower bound with a deliberately tested V2 range ending
   before V3.
8. Separate core, PostgreSQL, CLI, test, and optional integration dependencies.
9. Add Ruff formatting/lint configuration.
10. Add strict BasedPyright configuration and type-test discovery.
11. Add pytest warning, timeout, and marker configuration.
12. Add Hypothesis for state-machine/property tests.
13. Create CI jobs for lint, typing, unit tests, PostgreSQL integration, minimum
    dependencies, latest dependencies, and installed-wheel smoke tests.

### Suggested dependency groups

```text
core: pydantic
postgres: sqlalchemy, alembic, psycopg
cli: only dependencies truly required by the CLI
test: pytest, pytest-asyncio, hypothesis, pytest-cov, basedpyright, ruff
```

Do not make `python-dotenv`, FastAPI, DBOS, or a PostgreSQL driver unconditional core
dependencies.

### Required tests

- `import eventic` under the minimal core install.
- Public symbol snapshot.
- Import graph test.
- Wheel-content assertion for `py.typed` and migrations.
- Sdist exclusion assertion for `.scratch` and caches.

### Exit gate

- A wheel and sdist build reproducibly.
- The minimal wheel imports in a clean virtual environment.
- CI exists and all scaffold checks pass.
- No legacy public runtime code remains reachable.

## 9. Phase 2 — Implement foundational value types and errors

### Outcome

Identity, JSON types, references, timestamps, and failures are precise before any
store or Pydantic orchestration exists.

### Steps

1. Implement validated `StreamName` and `ComponentId` value types.
2. Implement opaque `CommandId` without assuming UUID format.
3. Implement `AggregateKey(stream, id)` and `RevisionKey(stream, id, revision)` as
   frozen, slotted dataclasses.
4. Implement deterministic revision ID generation:

   ```text
   uuid5(EVENTIC_REVISION_NAMESPACE, f"{stream}:{aggregate_id}:{revision}")
   ```

5. Implement deterministic event ID generation from revision ID and event kind.
6. Implement deterministic auto aggregate-ID generation for idempotent creates using
   application ID, command ID, and zero-based operation ordinal; freeze vectors in
   tests.
7. Define recursive `JsonValue`, `JsonObject`, and JSON-pointer types.
8. Implement UTC-aware timestamp validation and forbid naive datetimes in envelopes.
9. Implement the public error hierarchy.
10. Give concurrency/idempotency errors structured fields rather than only strings.
11. Ensure error rendering never includes document payloads or credentials by
    default.

### Minimum public errors

```text
EventicError
├── ConfigurationError
│   └── ApplicationCompileError
├── UsageError
├── AggregateNotFound
├── RevisionNotFound
├── ConcurrencyConflict
├── IdempotencyConflict
├── CommitRejected
├── SchemaEvolutionError
├── StorageError
├── DeliveryError
└── SerializationContractError
```

### Required tests

- Same UUID in different streams produces different revision IDs.
- Revision ID is stable across processes.
- Negative revisions and invalid stream names are rejected.
- Naive timestamps are rejected.
- Error `repr`, `str`, and structured fields contain no state payload.
- Static typing preserves UUID and revision-number distinctions where intended.

### Exit gate

- These modules have no SQLAlchemy dependency.
- Identity vectors are frozen in fixtures so future changes are loud.
- All error classes have documented triggering conditions.

## 10. Phase 3 — Implement canonical Pydantic documents

### Outcome

One function can turn supported Pydantic input into a sealed, deterministic,
round-trippable canonical document.

### Core types

```python
@dataclass(frozen=True, slots=True)
class CanonicalDocument[StateT]:
    value: StateT
    json_value: JsonObject
    payload: bytes
    sha256: bytes
```

```python
@dataclass(frozen=True, slots=True)
class DocumentSchema[StateT]:
    state_type: type[StateT]
    adapter: TypeAdapter[StateT]
    storage_schema: JsonObject
    fingerprint: str
```

### Canonicalization pipeline

Implement one `seal_document()` function with this exact conceptual sequence:

1. Validate Python input through the cached stream `TypeAdapter`.
2. Run declared deterministic normalizers.
3. Validate again; do not trust normalizer return types.
4. Serialize in JSON mode with:
   - `by_alias=False`
   - `exclude_computed_fields=True`
   - `round_trip=True`
   - `warnings="error"`
   - no `exclude_unset`, `exclude_defaults`, or `exclude_none`
   - a fixed, documented storage serialization context
5. Require the serialized root to be a JSON object for 1.0 streams.
6. Canonicalize that JSON value using the selected RFC 8785 implementation.
7. Hydrate a fresh model from the canonical bytes.
8. Verify semantic equivalence between the validated candidate and hydrated value.
9. Serialize and canonicalize the hydrated value again.
10. Require byte-for-byte idempotence between the two canonical payloads.
11. Compute SHA-256 from the canonical payload.
12. Return the freshly hydrated value as the committed value, never the caller's
    original instance.

### Semantic equivalence

Default equivalence for 1.0 is Pydantic model equality after requiring the same
concrete state type. If a model's private attributes or custom equality make this
unsuitable, allow a stream to declare an explicit `RoundTripEquivalence[T]` with a
stable ID included in the stream manifest. Never silently disable the check.

### Storage schema fingerprint

1. Generate validation and serialization schemas through public Pydantic APIs.
2. Derive a storage-schema projection that excludes presentation-only titles,
   descriptions, and examples but includes durable field names, requiredness,
   constraints, union/discriminator structure, and serialization shape.
3. Canonicalize and hash that projection.
4. Record the Pydantic version separately; do not hide it inside the fingerprint.
5. Add golden fixtures for representative schemas.

### Required adversarial tests

- Nested mutable lists, dicts, and nested models.
- `computed_field` values.
- Input and serialization aliases.
- Field and model validators.
- Field and model serializers.
- `UUID`, aware datetime, date, time, decimal, enum, bytes, URLs, and discriminated
  unions.
- `SecretStr` and other intentionally lossy serializers.
- Invalid defaults that Pydantic normally does not validate until supplied.
- Extra fields under forbid/ignore/allow configurations.
- Non-JSON arbitrary types.
- Serializer warnings promoted to errors.
- Non-idempotent serializers.
- Locale, timezone, and process restart determinism.

### Exit gate

- A sealed document can be persisted as bytes and reconstructed to the same semantic
  value in a fresh process.
- Lossy serializers fail with `SerializationContractError`.
- Computed fields appear in public serialization when Pydantic requests them but
  never in canonical storage bytes.
- Canonicalization has property tests and published golden vectors.

## 11. Phase 4 — Implement streams, commands, and envelopes

### Outcome

Users can declare typed streams and construct pure commands without a store.

### Stream

Implement immutable `Stream[StateT]` with:

```python
Stream(
    state_type,
    *,
    name,
    schema_version,
    upcasters=(),
    policies=(),
    normalizers=(),
    projections=(),
    equivalence=None,
)
```

Validation at construction:

- `state_type` is a concrete `BaseModel` subclass.
- Stream name is valid and explicit.
- Schema version is positive.
- Component declarations are copied into tuples.
- No database, registry, adapter cache, or mutable class attribute is touched.

### Commands

Implement pure frozen command values:

```python
Create[StateT]
Replace[StateT]
Change[StateT]
CommitBatch
```

Required command data:

- Stream reference/name.
- Proposed state or changes.
- Aggregate ID where relevant.
- Expected revision for replace/change.
- Typed metadata input.
- Optional command ID at the batch boundary.

`Change` supports top-level model-field changes only. It must reject managed envelope
names and unknown fields before store I/O. Nested changes use `edit()` plus `replace()`.

### Public envelopes

Implement generic Pydantic models:

```python
class Revision[StateT, MetaT = NoMetadata](BaseModel):
    id: UUID
    revision: NonNegativeInt
    revision_id: UUID
    stored_schema_version: PositiveInt
    interpreted_schema_version: PositiveInt
    stored_metadata_schema_version: PositiveInt
    interpreted_metadata_schema_version: PositiveInt
    committed_at: AwareDatetime
    value: StateT
    metadata: MetaT


class Committed[StateT, MetaT = NoMetadata](BaseModel):
    event_id: UUID
    kind: Literal["created", "changed", "replaced"]
    current: Revision[StateT, MetaT]
    previous: Revision[StateT, MetaT] | None
    changed_paths: tuple[str, ...]
    command_id: str | None
```

Envelope metadata fields are frozen against reassignment. Document values are
detached rather than promised to be recursively immutable.

Attach the canonical payload privately only if needed to implement `Revision.edit()`;
it must never enter equality, JSON Schema, or serialization. For 1.0, keep envelopes
completely transportable and implement `Revision.edit()` as a deep copy of `value`.
The draft is untrusted until `replace()` runs the complete canonicalization pipeline.
Do not attach adapters, stores, sessions, or canonical payloads to public envelopes.

Define `MetaT` with a default of `NoMetadata`, an empty frozen Pydantic model. An
application selecting custom metadata must declare its metadata type, schema version,
and metadata upcaster chain just as a stream declares state evolution.

### Required tests

- Construction performs no I/O.
- Stream declarations do not alter model fields or JSON Schema.
- Managed fields cannot enter state through command helpers.
- Commands are pure and hashable where appropriate.
- `Revision[Todo]` generates a useful JSON Schema and FastAPI-compatible shape.
- Envelope timestamps and revision numbers validate.
- Editing creates a deeply detached state value.
- Mutating one returned value cannot alter another envelope.

### Exit gate

- The declaration and command examples in section 5 work without persistence.
- Type fixtures infer state types through stream, command, and revision values.
- No module-level mutable registry exists.

## 12. Phase 5 — Implement the application compiler and extension expansion

### Outcome

An explicit `Application` compiles into one immutable, inspectable plan and rejects
invalid combinations before runtime.

### Steps

1. Implement frozen declaration types for subscriptions, policy bindings, observer
   bindings, projections, resources, and delivery routes.
2. Implement `Contributions` and the narrow `Extension` protocol described in
   `PLUGIN_FRAMEWORK.md`.
3. Implement `Application` as an immutable copy of user declarations.
4. Expand extensions exactly once in declaration order.
5. Reject extensions that return other extensions or unknown contribution types.
6. Resolve references only to streams installed in the same application.
7. Enforce explicit uniqueness for stream, extension, subscription, projection,
   layout, and transform IDs.
8. Build/cache one `DocumentSchema`/`TypeAdapter` per stream.
9. Compile upcaster chains and storage-schema fingerprints.
10. Inspect handler type annotations and sync/async compatibility.
11. Resolve `Annotated` field markers using public Pydantic field metadata.
12. Ask the supplied store for declared capabilities without performing writes.
13. Collect independent compilation failures and report them together.
14. Freeze resolved mappings and tuples into `ApplicationPlan`.
15. Implement deterministic `describe()`, `schema_manifest()`, and
    `storage_manifest()` output.

### Application plan contents

```text
application ID/version
runtime mode
stream plans keyed by stable stream name
compiled document schemas/adapters
compiled evolution chains
compiled metadata schema/adapter/evolution chain
ordered policies and normalizers
typed metadata schema/providers
subscriptions keyed by stable ID
delivery drivers/routes
projections and physical requirements
runtime resources and lifecycle order
store capabilities
schema/storage manifests
distribution and dependency version diagnostics
```

The runtime plan may contain callables and resources. Serializable manifests must
contain only non-secret descriptive data.

### Required tests

- Duplicate IDs across direct and extension contributions.
- Missing stream references.
- Deterministic extension expansion.
- Extension import/expansion causes no global mutation or I/O.
- Aggregated diagnostics contain correct component paths.
- Application input collections may be mutated afterward without changing the plan.
- Plan descriptions are stable across processes.
- A sync plan rejects coroutine handlers.
- An async plan rejects unsupported sync handlers unless explicitly adapted.
- Unsupported store capabilities fail compilation.
- Manifest rendering redacts DSNs, clients, credentials, and keys.

### Exit gate

- A non-persistent application with policies, observers, subscriptions, and one
  extension compiles deterministically.
- The resulting plan is immutable and completely inspectable.
- The compiler has no module scanning or global registry fallback.

## 13. Phase 6 — Build `MemoryStore` as the executable reference contract

### Outcome

The complete logical store behavior exists without SQL, establishing a reference for
all later store implementations.

### Internal store data types

Define store-facing values independent of public Pydantic envelopes:

```python
CanonicalRevision
StoredRevision
StoredHead
CommandClaim
CommandResultReference
DeliveryIntent
ProjectionMutation
CommitRequest
CommitResult
```

`CanonicalRevision` contains canonical bytes and JSON value produced by phase 3. It
does not contain a caller-owned model.

### Transaction contract

The store owns atomic commit. Its protocol should be coarse enough that the runtime
cannot accidentally pair intermediate writes from different stores:

```python
class Store(Protocol):
    capabilities: StoreCapabilities

    def commit(self, request: CommitRequest) -> CommitResult: ...
    def get_head(self, key: AggregateKey) -> StoredHead | None: ...
    def get_revision(self, key: RevisionKey) -> StoredRevision | None: ...
    def history(self, key: AggregateKey, page: Page) -> PageResult[StoredRevision]: ...
```

Commit policies in 1.0 are pure checks over the normalized proposal, previous
revision, and typed metadata. Cross-aggregate/database invariants belong in explicit
database constraints or application services until a real second use case justifies
a separately designed transactional-policy contract. Do not expose a session-like
object through the policy API.

### Memory implementation

1. Store revision rows append-only in dictionaries keyed by `RevisionKey`.
2. Store heads by `AggregateKey`.
3. Store command results by command ID and request digest.
4. Store delivery intents by deterministic intent ID.
5. Use one lock around atomic commit initially.
6. Build changes in a private copy or mutation journal.
7. Publish all changes only after every validation succeeds.
8. Support deterministic fault injection before each publication phase.
9. Return deep/canonical copies on every read.
10. Implement pagination from the start; never return an unbounded store iterator by
    default.

### Required store semantics

- Create succeeds only with no head and becomes revision 0.
- Change/replace requires an existing head equal to `expected_revision`.
- Next revision is exactly `expected_revision + 1`.
- No command can choose its target revision.
- Replaying a command ID with the same request digest returns the stored results.
- Reusing a command ID with a different digest raises `IdempotencyConflict`.
- A batch either publishes every revision/head/intent/result or none.
- Multiple operations targeting the same aggregate in one batch are either forbidden
  initially or explicitly ordered; choose and test one rule. Prefer forbidding them
  in 1.0 to keep batch expectations unambiguous.
- Delivery intent identity is unique by `(subscription_id, revision_id)`.

For `Create`, the command records whether its optional aggregate ID was explicit or
automatically generated. If the enclosing batch has a command ID, command preparation
derives a stable aggregate UUID from application ID, command ID, and operation ordinal.
Without a command ID it generates UUIDv4 once. The resulting aggregate ID is part of
the logical request digest, so replay is deterministic across processes.

### Required tests

- All create, change, replace, get, exact read, history, and pagination cases.
- Same aggregate UUID in two streams.
- Unsaved/fabricated/stale revisions.
- Same-base writers with one winner.
- Command replay and command conflict.
- Batch rollback at every injected fault point.
- Duplicate/overlapping subscriptions produce distinct intents by subscription ID.
- Returned/read state is detached.
- Concurrent threads sharing one `MemoryStore`.
- Head deletion plus exact rebuild equivalence.

### Exit gate

- `MemoryStore` passes the first version of `testing.store_contract`.
- Fault injection proves atomic visibility.
- No test relies on implementation object identity.

## 14. Phase 7 — Implement the sync commit and read runtime

### Outcome

The complete user-facing API works against `MemoryStore` through the sealed commit
pipeline.

### Commit orchestration

Implement the sync pipeline in one orchestration module:

1. Resolve the command batch against `ApplicationPlan`.
2. Load current canonical heads through the target store.
3. Check create/update shape and expected revisions.
4. Reconstruct prior Pydantic values from canonical payloads.
5. Build candidate state:
   - create: supplied model/input
   - replace: supplied complete state
   - change: merge top-level changes into a freshly hydrated prior canonical JSON
6. Seal every candidate through the phase 3 pipeline.
7. Validate typed metadata and capture stable command context.
8. Run commit policies in declared order.
9. Compute `changed_paths` from prior/current canonical JSON, not raw kwargs.
10. Construct `CommitRequest` containing only canonical internal values.
11. Let the store atomically assign revision numbers/timestamps and commit.
12. Reconstruct public `Revision` and `Committed` envelopes from the store result.
13. Return results to the caller only after store commit succeeds.
14. Run observers and inline subscriptions after result construction.
15. Report observer failures through an explicit sink without changing commit success.

### Collection API

`Runtime.collection(stream)` returns a typed `Collection[T, MetaT]` bound to exactly
one runtime/store:

```python
class Collection[StateT, MetaT]:
    def create(... ) -> Revision[StateT, MetaT]: ...
    def change(... ) -> Revision[StateT, MetaT]: ...
    def replace(... ) -> Revision[StateT, MetaT]: ...
    def get(... ) -> Revision[StateT, MetaT]: ...
    def history(... ) -> Page[Revision[StateT, MetaT]]: ...
```

Convenience methods construct a command and call the same `Runtime.commit()` used by
atomic batches. There is only one write implementation.

### Observer semantics

- Observer order is compiled declaration order.
- Each observer receives the same logical event envelope.
- Observer mutation cannot affect later observers; pass a newly hydrated or safely
  detached envelope when necessary.
- Observer exceptions are recorded with component ID and event ID.
- An optional strict test policy may re-raise observer failures after recording, but
  production default preserves committed success.

### Required tests

- All section 5 examples.
- Pydantic validation error preservation.
- Normalizer output revalidation.
- Policy veto before any store mutation.
- Canonical changed paths after Pydantic coercion/normalization.
- Event/current/previous values match exact store reads.
- `committed_at` is identical in returned and observed envelopes.
- Mutating the input or returned revision after commit changes no stored result.
- Inline observer failure semantics.
- Atomic multi-stream batch.
- Runtime A cannot use a collection or command bound to incompatible Application
  Plan B.

### Exit gate

- The complete sync API passes against `MemoryStore`.
- The seven original release blockers are impossible through the new public API.
- Every write flows through one orchestration function and one store atomic commit.

## 15. Phase 8 — Design the PostgreSQL schema and baseline migration

### Outcome

One production schema represents every invariant explicitly and is packaged with the
library.

### Use SQLAlchemy Core

Prefer SQLAlchemy Core tables and explicit statements over mutable ORM entities.
Append-only revisions, conditional head updates, leases, and command claims are
transactional records, not domain objects.

### Revisions table

Suggested columns:

```text
eventic_revisions
  stream                 text        not null
  aggregate_id           uuid        not null
  revision               bigint      not null check revision >= 0
  revision_id            uuid        not null unique
  kind                    text        not null check in created/changed/replaced
  schema_version          integer     not null check schema_version > 0
  layout_id               text        not null
  layout_version          integer     not null check layout_version > 0
  transform_chain         jsonb       not null
  physical_payload        bytea       not null
  canonical_sha256        bytea       not null
  metadata_schema_version integer     not null check metadata_schema_version > 0
  metadata_transform_chain jsonb      not null
  metadata_payload        bytea       not null
  metadata_sha256         bytea       not null
  command_id              text        null
  committed_at            timestamptz not null

  primary key (stream, aggregate_id, revision)
```

Add a database trigger that rejects update/delete of revision rows for the application
role. Provide an explicitly privileged administrative path for disaster recovery;
normal Eventic code never disables the trigger.

### Heads table

```text
eventic_heads
  stream                 text        not null
  aggregate_id           uuid        not null
  revision               bigint      not null
  revision_id            uuid        not null
  schema_version          integer     not null
  physical_payload        bytea       not null
  canonical_sha256        bytea       not null
  transform_chain         jsonb       not null
  state                   jsonb       null
  metadata_transform_chain jsonb      not null
  metadata_payload        bytea       not null
  metadata_sha256         bytea       not null
  metadata_schema_version integer     not null
  committed_at            timestamptz not null

  primary key (stream, aggregate_id)
  foreign key (stream, aggregate_id, revision)
    references eventic_revisions
```

The head stores a full snapshot payload even when the revision log uses deltas. The
JSONB `state` is an optional query projection of the canonical head payload and is
never independently authoritative. It may be `NULL` when whole-document encryption
or another transform prevents safe JSON projection. Head reads reverse the transform
chain and hydrate from canonical bytes.

### Commands table

```text
eventic_commands
  application_id          text        not null
  command_id              text        not null
  request_sha256          bytea       not null
  result_references       jsonb       not null
  committed_at            timestamptz not null

  primary key (application_id, command_id)
```

The request digest covers ordered logical operations and durable metadata but excludes
database-assigned timestamps. Define its canonical representation and golden vectors.

### Delivery intents table

```text
eventic_delivery_intents
  intent_id               uuid        primary key
  application_id          text        not null
  subscription_id         text        not null
  stream                  text        not null
  aggregate_id            uuid        not null
  revision                bigint      not null
  queue                   text        not null
  status                  text        not null
  available_at            timestamptz not null
  lease_token             uuid        null
  lease_owner             text        null
  lease_expires_at        timestamptz null
  attempts                integer     not null default 0
  last_error_code         text        null
  last_error_message      text        null
  last_error_at           timestamptz null
  created_at              timestamptz not null
  delivered_at            timestamptz null

  unique (application_id, subscription_id, stream, aggregate_id, revision)
  foreign key (stream, aggregate_id, revision)
    references eventic_revisions
```

Add indexes for claimable work, expired leases, queue/status, subscription/status, and
dead-letter inspection.

### Manifest table

```text
eventic_manifest
  application_id          text        not null
  component_type          text        not null
  component_id            text        not null
  version                 text        not null
  fingerprint             text        not null
  manifest                jsonb       not null
  installed_at            timestamptz not null

  primary key (application_id, component_type, component_id)
```

### Migration requirements

1. Create one clean 1.0 baseline revision.
2. Do not include legacy downgrade code pretending to recover an incompatible
   runtime format.
3. If downgrade is unsafe, raise with a clear irreversible-migration message.
4. Use package-resource lookup for Alembic scripts.
5. Test `upgrade`, `check`, repeat upgrade, and documented downgrade behavior.
6. Make `create_all` either private/test-only or prove exact parity with migrations.
   Prefer migrations as the one production and integration-test schema source.

### Required tests

- Every uniqueness, foreign key, check constraint, and append-only trigger.
- Alembic schema versus SQLAlchemy metadata parity.
- Fresh schema on minimum and latest supported PostgreSQL.
- Migration resources from an installed wheel.
- Timezone-aware timestamp round trips.
- No SQLite conditional branches.

### Exit gate

- The migration-created database is the only database used by PostgreSQL tests.
- `alembic check` is clean.
- The wheel can upgrade an empty PostgreSQL database without source-tree files.

## 16. Phase 9 — Implement synchronous `PostgresStore`

### Outcome

The PostgreSQL backend passes the same store contract as `MemoryStore` under real
concurrency and injected failures.

### Transaction algorithm

For a non-idempotent single-aggregate commit:

1. Begin one database transaction.
2. Lock the existing head with `SELECT ... FOR UPDATE` when updating.
3. For create, verify absence; rely on the composite primary key for the final race.
4. Compare the locked durable revision to `expected_revision`.
5. Assign `0` for create or `head.revision + 1` for update.
6. Ask PostgreSQL for one authoritative UTC commit timestamp.
7. Encode the already canonical payload through the configured physical layout and
   transforms.
8. Insert the immutable revision row.
9. Upsert/advance the head only from that canonical revision.
10. Insert all delivery intents using explicit subscription IDs.
11. Insert the command result if a command ID exists.
12. Commit.
13. Return only data captured by `RETURNING` or reread inside the same transaction.

For a batch:

1. Resolve and sort distinct aggregate keys before locking to avoid deadlocks.
2. Lock all existing heads in sorted order.
3. Validate every expected revision.
4. Reject duplicate aggregate targets in the same batch for 1.0.
5. Use one authoritative batch commit timestamp.
6. Insert revisions, heads, intents, and command result in deterministic operation
   order.
7. Roll back the entire batch on any failure.

### Idempotent command algorithm

1. Compute the canonical request digest before opening retryable transaction work.
2. Acquire command exclusivity using a database mechanism with documented collision
   behavior, such as a transaction-scoped advisory lock derived from a cryptographic
   command-ID hash.
3. Read `eventic_commands` after obtaining exclusivity.
4. If a row exists:
   - same digest: load and return the referenced revisions
   - different digest: raise `IdempotencyConflict`
5. If no row exists, execute the batch and insert its result references atomically.
6. Test two concurrent first attempts using the same command ID.

For auto-ID idempotent creates, generate the aggregate ID deterministically before
computing the logical request digest. The command table still determines whether to
execute or replay the batch; deterministic ID generation makes policy evaluation,
error reporting, and concurrent first attempts refer to the same aggregate.

Do not derive aggregate identity from command identity. A replay returns the stored
result references.

### Retry rules

- Retry only recognized transient database errors.
- Retry the entire transaction with the same captured command context.
- Never retry a non-idempotent operation after an ambiguous connection loss unless
  the store can prove transaction outcome.
- With a command ID, resolve ambiguous outcomes by rereading the command table.
- Bound retries and expose retry telemetry.

### Required tests

- Run every `testing.store_contract` test against PostgreSQL.
- Two concurrent creates for one aggregate.
- Many same-base writers: one winner, loud losers.
- Same command ID/same input concurrently.
- Same command ID/different input concurrently.
- Batch lock order with reversed caller order.
- Failures after revision insert, head write, intent staging, and command insert.
- Connection loss before and after commit acknowledgment.
- Append-only trigger behavior.
- Read, history, and head reconstruction after process restart.
- Shared store across threads without shared sessions.

### Exit gate

- Memory and PostgreSQL stores produce equivalent logical results for generated
  command sequences.
- No SQLAlchemy session is stored in process-global or context-local state.
- PostgreSQL failure injection demonstrates atomicity at every write boundary.

## 17. Phase 10 — Implement schema evolution and historical reads

### Outcome

Every stored schema version has an explicit deterministic path to the current
Pydantic state type.

### Steps

1. Define `Upcaster` as a one-version JSON-object transform.
2. Require `to_version == from_version + 1`.
3. Compile exactly one unambiguous chain from every supported version to current.
4. Apply upcasters after physical layout/transform decoding and before current-model
   validation.
5. Validate each intermediate output as a JSON object.
6. Validate the final value through the current stream adapter.
7. Keep original canonical payload/digest associated with the historical revision;
   upcasted current-state views are derived reads.
8. Expose both `stored_schema_version` and `interpreted_schema_version` on public
   revision envelopes.
9. Apply the same versioned-evolution discipline to typed commit metadata.
10. Include state and metadata evolution chains/fingerprints in plan inspection.
11. Add a schema-diff command that requires explicit acknowledgement of a changed
    storage fingerprint.

### Fixture strategy

Store immutable fixture directories:

```text
tests/fixtures/evolution/todos/
├── v1/schema.json
├── v1/documents.jsonl
├── v2/schema.json
├── v2/documents.jsonl
└── expected-current.jsonl
```

Fixtures must be produced by the actual old release or checked-in canonical vectors,
not regenerated automatically by the current code during tests.

### Required tests

- Missing, duplicate, skipped, and cyclic transitions.
- Field rename, default introduction, union change, nested model change, and enum
  evolution.
- Determinism across repeated/process-separated reads.
- An upcaster returning a non-object or invalid current state.
- Upcaster exceptions include stream/version context without full payload leakage.
- Historical exact reads and history pagination across mixed schema versions.
- Head rebuild through evolution.

### Exit gate

- Mixed-version fixture streams load predictably.
- Application compilation fails before runtime when evolution is incomplete.
- No hydration interceptor or alias trick substitutes for upcasting.

## 18. Phase 11 — Implement subscriptions, outbox, and workers

### Outcome

Inline and durable event delivery share one event contract, and durable work has an
operable at-least-once state machine.

### Subscription compilation

1. Require explicit subscription IDs.
2. Compile event source filters (`created`, `changed`, `replaced`, or all).
3. Validate handler annotations against the stream event type.
4. Compile delivery requirements against store capabilities.
5. Reject duplicate IDs and missing queue bindings.
6. Materialize deterministic intent IDs from subscription ID and revision ID.

### Event reconstruction

Construct `Committed[T, MetaT]` from stored revision references:

- Load current exact revision.
- Load previous exact revision when it exists.
- Apply layouts, transforms, and schema evolution.
- Compute or verify canonical `changed_paths`.
- Use stored `committed_at` and command ID.
- Produce the same logical envelope used immediately after commit.

Do not store pickled envelopes or Python-qualified handler names as durable payloads.

### Claim algorithm

For PostgreSQL workers:

1. Select eligible rows by queue/status/available time.
2. Use `FOR UPDATE SKIP LOCKED` in bounded batches.
3. Set a new random lease token, owner, expiration, and increment attempts.
4. Commit the claim transaction before executing handlers.
5. Execute each handler outside the claim transaction.
6. Acknowledge with a compare-and-set on intent ID plus lease token.
7. On failure, compare-and-set the active lease into pending/backoff or dead-letter.
8. Expired leases become claimable without a destructive cleanup pass.

### Retry and dead-letter semantics

- Retry policy is explicit in the compiled delivery route.
- Backoff uses bounded exponential growth with jitter.
- Attempts and next availability are durable.
- Error messages are sanitized and length-limited.
- Exhaustion transitions to `dead_letter`, never silent deletion.
- Redrive creates a durable state transition and audit record.
- Acknowledgment failure after handler success can cause redelivery; document this as
  at-least-once.

### Worker results

Return a structured result:

```python
class DrainResult(BaseModel):
    claimed: int
    delivered: int
    retried: int
    dead_lettered: int
    lease_lost: int
    retained: int
```

CLI exit status is nonzero when configured failure thresholds are met. Never print
“drained” when work was retained or merely rescheduled.

### Required tests

- Inline/durable envelope equivalence.
- Overlapping subscriptions for one handler function.
- Renamed Python functions with stable subscription IDs.
- Missing handler in a fresh process produces a compile/startup failure.
- Multiple concurrent workers.
- Crash after claim, during handler, after side effect, and before acknowledgment.
- Lease expiration and token mismatch.
- Retry exhaustion, dead-letter inspection, and redrive.
- Async handler handling in async workers and rejection in sync workers.
- No secrets or raw DSNs in rows/logs/errors.
- Installed-wheel worker loading an explicit application target.

### Exit gate

- Durable delivery passes `testing.delivery_contract` against Memory and PostgreSQL
  stores.
- The worker survives process restart with only application target and store config.
- Documentation uses “best effort” and “at least once” precisely.

## 19. Phase 12 — Implement projections and query ergonomics

### Outcome

Useful latest-state queries are explicit, named, validated against Pydantic schemas,
and rebuildable.

### Steps

1. Implement `Indexed()` as inert `Annotated` metadata.
2. Resolve marked paths during application compilation.
3. Define named `Projection` declarations for compound or transformed keys.
4. Compile PostgreSQL projections to JSONB expression/generated-column indexes as
   appropriate.
5. Record every projection ID, version, definition fingerprint, and backend plan in
   the manifest.
6. Expose queries through compiled projection handles, not arbitrary dotted kwargs:

   ```python
   page = todos.find(ByStatus, "active", page=Page.first(50))
   ```

7. Validate query values using the Pydantic field/type adapter associated with the
   projection.
8. Define null versus missing semantics explicitly.
9. Require pagination and stable ordering.
10. Build projection rebuilds from canonical heads/revisions in bounded chunks.
11. Remove orphan projection state during rebuild.

### Required tests

- Field path existence and type validation.
- Alias handling: storage field names, not presentation aliases.
- Explicit null versus missing behavior.
- Compound keys and stable pagination.
- Projection ID/fingerprint conflicts.
- Rebuild equivalence before/after deleting derived projection state.
- Large dataset query plans use the expected index.
- Encrypted/indexed field conflict detected at compilation.

### Exit gate

- No public generic `.where(**dotted_paths)` remains.
- Every queryable path appears in plan inspection and migration output.
- Projection rebuilds are bounded and deterministic.

## 20. Phase 13 — Implement physical layouts and payload transforms

### Outcome

Storage optimization and reversible byte transforms are extensible without gaining
authority over logical state.

### Snapshot layout first

1. Implement `Snapshots` as layout ID `eventic.snapshot`, version 1.
2. Store canonical bytes directly as physical payload.
3. Reconstruction returns those exact bytes.
4. Use it as the only layout until the full store/runtime suite is green.

### Layout registry

The compiled store keeps decoders keyed by `(layout_id, layout_version)`. Historical
decoders remain available as long as rows using them exist. Removing a required
decoder is a schema-check/startup error.

### Checkpointed delta layout

Only after snapshots are proven:

1. Select a versioned JSON patch representation.
2. Compute patches from prior canonical JSON to current canonical JSON.
3. Store full checkpoints at deterministic intervals.
4. Include an explicit base revision/checkpoint reference.
5. Reconstruct bounded windows and verify the final canonical digest.
6. Make history perform one forward fold rather than N point reconstructions.
7. Make live head update and head rebuild call the same reconstruction/projector
   implementation.

### Payload transforms

1. Implement the stable `PayloadTransform` protocol over physical bytes.
2. Store transform ID/version chain on every row.
3. Apply transforms in declaration order and reverse them in reverse order.
4. Add compression only after byte round-trip conformance passes.
5. Add encryption only with explicit key identifiers and external key providers.
6. Never serialize a key, credential, or provider configuration into the row or
   manifest.
7. Validate transform/layout combinations at compilation.

### Required tests

- Snapshot exact-byte round trip.
- Delta histories around every checkpoint boundary.
- Missing/corrupt base/checkpoint rows.
- Random JSON document sequences through delta folding.
- Mixed layout versions in one stream.
- Layout migration without changing logical schema version.
- Compression/encryption round trips and corrupt ciphertext.
- Historical key/version lookup.
- Head rebuild equals original digest for every aggregate.
- Property tests compare snapshot and delta stores for identical logical histories.

### Exit gate

- All layouts pass `testing.layout_contract`.
- All transforms pass `testing.transform_contract`.
- No runtime or head code switches on known layout IDs outside the compiled layout
  registry.

## 21. Phase 14 — Implement async parity

### Outcome

Async applications receive a native async store/runtime/worker surface with the same
semantics as sync code.

### Steps

1. Define `AsyncStore` and async transaction/delivery protocols explicitly.
2. Implement `AsyncMemoryStore` with `asyncio.Lock` and the same logical engine.
3. Implement `AsyncPostgresStore` using SQLAlchemy's async Core path and a supported
   async psycopg driver.
4. Share immutable SQL statement builders and pure canonicalization functions.
5. Do not implement async by running the sync store in an implicit thread pool.
6. Implement `AsyncRuntime`, `AsyncCollection`, and `AsyncWorker`.
7. Support async policies, observers, resources, and handlers through async-specific
   protocols.
8. Require explicit adapters for sync callables used by async deployments.
9. Implement cancellation-safe transaction and resource cleanup.
10. Ensure no lock/session/transaction crosses asyncio task boundaries accidentally.

### Required tests

- Parameterize logical contract tests over sync and async surfaces where readable.
- Shared async store across many tasks.
- Cancellation before write, during database wait, after store commit, and during
  observer execution.
- Connection-pool exhaustion and recovery.
- Async worker lease behavior and cancellation.
- Runtime resource startup failure and reverse-order cleanup.
- Type fixtures prove awaitability and reject missing `await` where type checkers can.

### Exit gate

- Async Memory/PostgreSQL pass equivalent conformance tests.
- No coroutine is silently created and discarded.
- Sync APIs never unexpectedly return awaitables.

## 22. Phase 15 — Implement CLI, application loading, and operability

### Outcome

All operational workflows use the same explicit application and compiled plan as the
runtime.

### Application loader

1. Accept one `module:attribute` target.
2. Import the module and retrieve an `Application` or documented factory.
3. Never scan modules or rely on decorator side effects.
4. Validate that the target's plan can compile for the requested runtime/store.
5. Separate application declarations from secret store configuration.

### Commands

Implement:

```console
eventic --app pkg.module:application inspect
eventic --app pkg.module:application inspect --stream todos
eventic --app pkg.module:application schema export
eventic --app pkg.module:application schema check
eventic --app pkg.module:application schema diff
eventic --app pkg.module:application migrate
eventic --app pkg.module:application worker --queue search
eventic --app pkg.module:application deliveries list
eventic --app pkg.module:application deliveries dead-letter list
eventic --app pkg.module:application deliveries redrive --subscription ID
eventic --app pkg.module:application rebuild heads --stream todos
eventic --app pkg.module:application rebuild projections --stream todos
```

### CLI rules

- Every command supports machine-readable JSON output where useful.
- Exit codes distinguish configuration, schema drift, partial work, and runtime
  failure.
- Dry-run output lists exact targets and bounded batch sizes.
- Destructive administrative operations require explicit narrow confirmation.
- Database URLs and credentials are redacted in output and exceptions.
- Rebuild commands use leases/advisory locks to prevent overlapping incompatible
  rebuilds.
- Rebuilds are resumable and paginated.

### Required tests

- Fresh subprocess invocation from the built wheel.
- Invalid app target, missing attribute, wrong type, and compile failure.
- Schema check clean/drift exit codes.
- Migration from packaged resources.
- Worker reports exact `DrainResult` values and exit status.
- Redaction snapshots.
- Interrupted/resumed head and projection rebuilds.

### Exit gate

- Every documented command is executed in installed-wheel CI.
- Operators can inspect every component affecting commits and delivery.
- No CLI path requires the source checkout.

## 23. Phase 16 — Publish and prove the extension developer kit

### Outcome

Third parties can implement extensions using stable public contracts and run official
conformance suites.

### Steps

1. Finalize only those protocols exercised by at least two implementations or one
   implementation plus a meaningful reference test double.
2. Export `Contributions`, extension protocols, store capabilities, layout/transform
   protocols, delivery types, and projection types from intentional modules.
3. Keep internal commit request details private unless a store author genuinely needs
   them.
4. Package conformance helpers under `eventic.testing`.
5. Create a separate example package, such as `examples/eventic-search`, using normal
   package metadata and `py.typed`.
6. Implement the complete search extension from `PLUGIN_FRAMEWORK.md`.
7. Verify deterministic extension expansion and stable IDs.
8. Document supported and forbidden extension behavior.
9. Document version compatibility and deprecation policy for protocols.
10. Add an extension author checklist.

### Extension author checklist

- No module-level mutable registry.
- No import-time I/O.
- Explicit stable IDs.
- Typed configuration.
- No secret values in manifests or durable payloads.
- Deterministic contributions.
- Declared store capabilities.
- Sync/async mode explicit.
- Schema/migration contributions packaged in the extension wheel.
- Official conformance suite green.

### Required tests

- Example extension works from a separately built/installed wheel.
- Missing capability produces a useful compile error.
- Duplicate extension/contribution IDs are loud.
- Extension resources start/stop in deterministic order.
- Extension manifests contain distribution/version diagnostics.
- No automatic discovery occurs when the package is merely installed.

### Exit gate

- A developer can build an extension without importing private Eventic modules.
- Conformance test documentation is complete and executable.
- The example extension proves bundle, subscription, delivery, projection, schema,
  typing, packaging, and worker integration.

## 24. Phase 17 — Property, state-machine, and fault-injection hardening

### Outcome

Correctness is tested across operation sequences and failure points, not only happy
path examples.

### Stateful model testing

Use Hypothesis rule-based state machines with a simple mathematical oracle:

```text
oracle[(stream, id)] = [canonical revision 0, revision 1, ...]
```

Generate:

- Creates across streams and IDs.
- Valid and invalid changes.
- Stale retries.
- Command replays/conflicts.
- Multi-command batches.
- Serializer/validator coercions.
- History pages.
- Head deletion/rebuild.
- Worker claims, failures, lease expiry, and redrive.

After every operation assert:

- Store history equals the oracle.
- Head equals the last oracle revision.
- Exact reads equal their oracle revision.
- Revision numbers are contiguous.
- Digests and IDs are stable.
- Delivery intent count equals matching durable subscriptions.
- Rebuilding derived state changes nothing.

Run the same generated traces against Memory and PostgreSQL and compare normalized
results.

### Fault injection

Introduce named fault points at:

- Before/after policy evaluation.
- Before/after canonicalization.
- Before revision insert.
- After revision insert.
- After head advance.
- During each delivery-intent insert.
- Before command-result insert.
- Before database commit.
- After commit but before acknowledgment reaches the caller.
- Before/after observer invocation.
- Worker claim, handler, failure transition, and acknowledgment.

For every fault, assert the documented atomic or post-commit outcome.

### Concurrency testing

- Many writers to one aggregate.
- Many streams sharing UUIDs.
- Idempotent same-command races.
- Conflicting command races.
- Batches locking aggregate keys in opposing input orders.
- Concurrent workers and expired leases.
- Concurrent rebuild versus writes according to documented coordination rules.
- Sync threads and async tasks.

### Exit gate

- Long randomized runs find no divergence between log, head, exact reads, history,
  events, and rebuilds.
- Every named fault point has an asserted outcome.
- Concurrency tests are reliable enough to run in CI with bounded timeouts.

## 25. Phase 18 — Security and privacy hardening

### Outcome

The implementation has explicit controls for secrets, untrusted schemas, payload
sizes, and extension authority.

### Steps

1. Threat-model application loading, extension code, Pydantic annotation evaluation,
   database roles, delivery handlers, payload transforms, and CLI output.
2. Document that application and extension code is trusted code.
3. Add maximum canonical document, metadata, command batch, error message, and worker
   batch sizes.
4. Reject oversized payloads before holding database locks where possible.
5. Redact DSNs and known secret types centrally.
6. Ensure Pydantic validation errors exposed to logs do not dump complete sensitive
   payloads by default.
7. Use a restricted application DB role without DDL privileges.
8. Keep migration credentials separate from runtime credentials.
9. Test the append-only revision trigger under the runtime role.
10. Validate all JSON paths and identifiers before using them in generated SQL.
11. Keep SQL construction parameterized; projection compilation must not concatenate
    untrusted identifiers.
12. Make transform/key-provider failures non-revealing.
13. Add dependency vulnerability and license checks to release CI.
14. Produce an SBOM and provenance for releases if the distribution pipeline supports
    it.

### Required tests

- DSN/password/key redaction from exceptions, reprs, logs, manifests, and CLI.
- Oversized state, metadata, batch, and delivery error behavior.
- Malicious stream/component/index identifiers.
- Runtime DB role cannot mutate/delete revisions or run migrations.
- Worker error storage truncation and control-character handling.
- Installed but unconfigured extensions are never imported.

### Exit gate

- Security checklist is reviewed independently.
- No test fixture contains real credentials.
- Runtime and migration privilege separation is documented and proven.

## 26. Phase 19 — Performance and capacity engineering

### Outcome

Performance work preserves semantics and produces published operational limits.

### Benchmarks

Measure at minimum:

- Pydantic validate/seal latency by document size.
- Single create/update throughput.
- Contended aggregate update latency.
- Batch commit sizes.
- Latest and exact reads.
- Long-history reads for snapshots and deltas.
- Head/projection rebuild throughput.
- Outbox claim/delivery/ack throughput.
- Schema compilation startup time with many streams/extensions.

### Optimization order

1. Profile before changing code.
2. Cache `TypeAdapter` and compiled plan artifacts.
3. Batch SQL writes without weakening atomicity.
4. Keep history and rebuild operations paginated/chunked.
5. Add indexes based on measured query plans.
6. Optimize delta checkpoint intervals using representative workloads.
7. Keep canonical full head payloads even if log storage is optimized.
8. Add bounded caches only with explicit ownership and invalidation.

### Capacity limits

Document and enforce:

- Maximum document and metadata size.
- Maximum commands per batch.
- Maximum subscriptions/intents per revision.
- History page limits.
- Worker claim limits and lease duration bounds.
- Rebuild chunk limits.
- Supported stream/application catalog sizes.

### Exit gate

- Benchmarks run in a reproducible environment.
- No optimization bypasses validation, canonicalization, CAS, or intent staging.
- Operational defaults and limits are documented.

## 27. Phase 20 — Documentation and examples

### Outcome

Documentation describes the shipped implementation, not historical review markers or
aspirational behavior.

### Required documentation

1. One-sentence product thesis.
2. Five-minute Pydantic-first quick start.
3. State versus revision versus command mental model.
4. Create/change/replace/read/history examples.
5. Optimistic concurrency and idempotency.
6. Transactions and atomic batches.
7. Typed metadata.
8. Schema evolution.
9. Inline versus durable delivery guarantees.
10. Worker operations, retries, dead letters, and redrive.
11. Application compilation and inspection.
12. Projections and indexing.
13. Layouts/transforms and their versioning.
14. Extension author guide.
15. PostgreSQL migration/deployment guide.
16. FastAPI integration.
17. Sync and async API guides.
18. Testing with `MemoryStore` and conformance helpers.
19. Security and privacy guidance.
20. Troubleshooting/error reference.

### Examples

Ship small executable examples:

- Minimal sync todo application.
- Async FastAPI application.
- Durable outbox worker.
- Schema v1-to-v2 evolution.
- Atomic multi-stream batch.
- Custom policy and observer.
- External example extension package.

Run every example in CI from the installed wheel. Examples must create isolated
temporary resources and clean them up safely.

### Exit gate

- No documentation references removed APIs.
- Every guarantee is labeled precisely.
- Every command/example is executable in CI.

## 28. Phase 21 — Cutover and release engineering

### Outcome

The source tree contains only the new architecture and produces a trustworthy 1.0
release candidate.

### Removal checklist

Delete or replace:

- `Record` and `Draft` inheritance API.
- Ambient `connect()` and public unit-of-work state.
- Old codecs and codec-aware head code.
- Class-keyword configuration.
- Global stream/handler/queue registries.
- Generic interceptors.
- Decorator subscription registration.
- DBOS code that serializes a raw database URL.
- Old migrations and migration documentation.
- SQLite-specific claims and tests.
- Historical implementation diary comments in production modules.
- Stale examples and review-marker naming in tests.

Do not retain deprecated imports that silently emulate old behavior. Import failures
are clearer than a compatibility layer with subtly different semantics.

### Release-candidate validation

1. Run all lint, format, type, unit, property, integration, conformance, security, and
   installed-wheel suites.
2. Build sdist and wheel from a clean checkout.
3. Inspect archive contents against allowlists.
4. Install each artifact into a fresh environment.
5. Start PostgreSQL using the supported minimum version.
6. Load the sample application from the wheel.
7. Run packaged migrations.
8. Create, change, read, and inspect history.
9. Drain a durable subscription using a fresh worker process.
10. Exercise dead-letter and redrive.
11. Rebuild heads and projections and compare digests.
12. Repeat against the latest supported dependency/database versions.
13. Generate schema/storage manifests and release provenance.
14. Publish `1.0.0rc1` before final `1.0.0`.

### Exit gate

- The release matrix in section 29 is completely green.
- No blocker/high finding from `REVIEW.md` is reproducible.
- The API reference and package exports match the approved public surface.
- The repository and distribution contain no secrets or unintended scratch assets.

## 29. Mandatory validation matrix

### 29.1 Pydantic/state

- Mutable nested lists/dicts/models.
- Computed fields.
- Validation and serialization aliases.
- Field/model validators and serializers.
- Strict/coercing models.
- Defaults and default factories.
- UUID, aware datetime, date, decimal, enum, bytes, URLs, secrets.
- Nested/discriminated unions.
- Extra forbid/ignore/allow.
- Serialization warnings/errors and lossy round trips.
- Schema fingerprint stability.

### 29.2 Identity/concurrency/idempotency

- Same UUID across streams.
- Revision 0 creation and contiguous increments.
- Stale, fabricated, detached, and unsaved bases.
- One winner for same-base writers.
- Same command/same request replay.
- Same command/different request conflict.
- Ambiguous commit outcome recovery.
- Atomic batches and deterministic lock ordering.

### 29.3 Persistence/rebuild

- Exact/latest/history equivalence.
- Head equals last log revision.
- Delete/rebuild all heads.
- Remove orphan heads.
- Snapshot/delta logical equivalence.
- Layout and transform version mixtures.
- Fault after every transactional write stage.
- Runtime-role append-only enforcement.

### 29.4 Evolution

- Every supported stored version.
- Missing/duplicate evolution transitions.
- Final Pydantic validation.
- Old fixture reads in fresh processes.
- Mixed-version history/rebuild.

### 29.5 Delivery

- Inline/durable event equivalence.
- Stable subscription/intent identities.
- Multiple matching subscriptions.
- Concurrent workers.
- Claims, expired leases, lost leases.
- Handler success plus ack failure.
- Retry exhaustion, dead letter, redrive.
- Fresh worker application loading.
- Secret-free durable state.

### 29.6 Extensions

- Deterministic expansion.
- Duplicate/conflicting contributions.
- Capability checks.
- Sync/async compatibility.
- Resource startup/shutdown failures.
- Separate-wheel installation and typing.
- Schema/migration contributions.

### 29.7 Release

- Ruff and format clean.
- Strict type checks clean.
- Warnings as errors.
- Coverage threshold met on invariant kernel.
- Minimum/latest dependency matrix.
- Minimum/latest supported PostgreSQL matrix.
- Clean wheel/sdist contents.
- Installed-wheel migration/runtime/worker smoke tests.
- Documentation examples executed.

## 30. Recommended commit sequence

Keep implementation reviewable with commits approximately like:

```text
1. Record 1.0 architecture decisions and API fixtures
2. Replace package skeleton and establish quality gates
3. Add identity, JSON value types, and public errors
4. Add canonical Pydantic document sealing
5. Add streams, commands, and typed envelopes
6. Add application catalog and compiler
7. Add extension expansion and diagnostics
8. Add MemoryStore and store conformance suite
9. Add sync runtime and collection API
10. Add PostgreSQL baseline migrations
11. Add synchronous PostgresStore
12. Add command idempotency and atomic batches
13. Add schema evolution and fixtures
14. Add subscriptions and inline delivery
15. Add transactional outbox and worker state machine
16. Add named projections and PostgreSQL indexes
17. Add snapshot layout conformance
18. Add checkpointed delta layout
19. Add payload transforms
20. Add native async stores and runtime
21. Add CLI, inspection, migrations, and rebuilds
22. Add extension developer kit and example package
23. Add state-machine, concurrency, and fault testing
24. Add security and performance gates
25. Replace docs/examples and remove legacy surface
26. Validate and package 1.0 release candidate
```

If a commit cannot be summarized this narrowly, split it before review.

## 31. Milestone dependency map

```text
Contract/ADRs
    ↓
Package skeleton and tooling
    ↓
Identity + errors + canonical Pydantic documents
    ↓
Streams + commands + envelopes
    ↓
Application compiler + typed extensions
    ↓
MemoryStore reference contract
    ↓
Sync runtime vertical slice
    ↓
PostgreSQL schema + PostgresStore
    ├───────────────┐
    ↓               ↓
Schema evolution   Durable delivery
    └───────┬───────┘
            ↓
Projections + layouts + transforms
            ↓
Native async parity
            ↓
CLI + extension SDK + installed-wheel workflows
            ↓
Property/fault/security/performance hardening
            ↓
Documentation, cutover, release candidate
```

Do not parallelize work whose inputs are not stable. PostgreSQL and async work can
proceed in parallel only after the store contract and sync logical behavior are
frozen. Delivery and evolution may proceed in parallel after PostgreSQL's atomic
commit contract is proven.

## 32. First vertical-slice checkpoint

Before building outbox, plugins, deltas, async, or queries, require this small program
to pass against both Memory and PostgreSQL:

```python
class Todo(BaseModel):
    text: str
    done: bool = False


Todos = Stream(Todo, name="todos", schema_version=1)
app = Application(id="todo-service", streams=[Todos])
runtime = Runtime(app.compile(store=store, runtime="sync"))
todos = runtime.collection(Todos)

created = todos.create(Todo(text="learn"))
changed = todos.change(created, done=True)

assert created.revision == 0
assert changed.revision == 1
assert todos.get(created.id) == changed
assert todos.get(created.id, revision=0) == created
assert list(todos.history(created.id)) == [created, changed]
```

Then delete all head rows through a test-only administrative helper, rebuild them,
and rerun every assertion. Run two concurrent `change(created, ...)` calls and assert
one success plus one `ConcurrencyConflict`. Mutate every caller-held model before and
after commits and assert no stored value changes.

This checkpoint is the architectural proof. If it is not simple and reliable, stop
and repair the kernel before adding features.

## 33. Final completion checklist

- [ ] All fixed decisions have ADRs.
- [ ] Target API typing fixtures pass.
- [ ] Minimal core installation imports.
- [ ] Pydantic sealing is deterministic and lossless.
- [ ] User state contains no Eventic metadata fields.
- [ ] Application compilation is explicit and deterministic.
- [ ] Extension expansion has no side effects.
- [ ] MemoryStore passes store conformance.
- [ ] Sync runtime vertical slice passes.
- [ ] PostgreSQL migrations are packaged and authoritative.
- [ ] PostgresStore passes store and concurrency conformance.
- [ ] Command replay/conflict semantics pass.
- [ ] Atomic batches pass fault injection.
- [ ] Schema evolution fixtures pass.
- [ ] Inline/durable events are logically identical.
- [ ] Outbox leases, retries, dead letters, and redrive pass.
- [ ] Projection/index manifests and rebuilds pass.
- [ ] Snapshot and delta layouts pass the same logical history suite.
- [ ] Payload transforms round-trip and redact secrets.
- [ ] Native async parity passes.
- [ ] CLI works from an installed wheel.
- [ ] Example third-party extension works from a separate wheel.
- [ ] Stateful/property tests find no projection divergence.
- [ ] Every transactional fault point has the documented outcome.
- [ ] Security review and privilege tests pass.
- [ ] Benchmarks and capacity limits are published.
- [ ] Legacy runtime/API/migrations are removed.
- [ ] Documentation examples execute in CI.
- [ ] Release-candidate matrix is fully green.

## 34. Guiding implementation test

At every design or code review, ask:

> Does this change preserve one validated canonical document, one explicit store,
> one atomic commit decision, and one inspectable application plan?

If the answer is uncertain, do not merge the change. Reduce it until the invariant is
obvious in code and executable in a fault test.
