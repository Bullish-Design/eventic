# Eventic Rewrite — Plugin and Extension Framework

Date: 2026-08-05
Status: Proposed architecture for a ground-up, compatibility-breaking rewrite
Scope: extensibility, component boundaries, application compilation, Pydantic
integration, storage layouts, delivery, projections, operability, and third-party
packaging

## 1. Executive summary

The rewrite should remain highly extensible, but it should not have a general hook
that can reach through the commit pipeline. Extensibility should surround a small,
sealed correctness kernel.

The framework has two layers:

1. **Narrow runtime protocols** describe one behavior each: storing canonical
   revisions, checking a commit policy, observing a completed commit, projecting a
   document, or delivering an event.
2. **Extension bundles** are optional packaging conveniences. They contribute typed,
   declarative components to an `Application`; they do not receive privileged access
   to Eventic internals and they disappear after application compilation.

The resulting model is:

```text
Pydantic document
    + Stream declaration
    + explicit typed contributions
    + Store implementation
    + Application.compile(...)
    = immutable, validated ApplicationPlan
```

This preserves the best principle in the current concept:

> The plugin framework should be the type system, not a bespoke registry.

It strengthens that principle by eliminating class-level configuration, mutable
global registries, magic installation, priority integers, string capability tokens,
and generic interceptors.

## 2. Design goals

The framework must:

1. Keep all framework classes out of the user's Pydantic model inheritance tree.
2. Make the complete runtime configuration explicit and inspectable.
3. Reject incompatible components before the application begins serving traffic.
4. Preserve one canonical document across log, head, returned revision, and event.
5. Let third parties extend useful behavior without weakening core invariants.
6. Give every durable declaration a stable identity.
7. Make ordering visible rather than deriving it from import order or numeric
   priorities.
8. Support sync and async execution without silently ignoring awaitables.
9. Give CLI commands and workers the same application catalog as the web process.
10. Make extension behavior testable in isolation and through reusable conformance
    suites.
11. Permit physical storage optimizations without changing logical revision
    semantics.
12. Use Pydantic's public validation, serialization, generic-model, `Annotated`, and
    JSON Schema facilities rather than reproducing them.

## 3. Non-goals

The framework is not:

- A dependency-injection container.
- An import-time plugin discovery system.
- A setuptools entry-point autoloader.
- A generic middleware stack around arbitrary internal functions.
- An ORM event system.
- A way to redefine revision identity, concurrency, or transaction semantics.
- A way to make incompatible stores appear compatible.
- A capability-token language parallel to Python's type system.
- A mechanism for injecting fields or methods into user models.
- A compatibility layer for the current `Record` inheritance API.

Third-party packages may expose convenient factories and extension bundles, but the
application must import and install them explicitly.

## 4. Terminology

### 4.1 Component

A component is an object satisfying one narrow runtime protocol, such as
`CommitPolicy`, `CommitObserver`, `Delivery`, or `Projection`.

### 4.2 Contribution

A contribution is an immutable declaration added to an application catalog. A
subscription, projection, stream, policy binding, or observer binding is a
contribution.

### 4.3 Extension

An extension is a convenience object that expands into contributions. An extension
does not participate directly in the runtime pipeline.

### 4.4 Application catalog

The catalog is the explicit, uncompiled set of streams and contributions supplied by
the application.

### 4.5 Application plan

The plan is the immutable result of compiling a catalog against a concrete store and
runtime mode. It contains all resolved schemas, handlers, policies, projections,
delivery routes, component ordering, and store requirements.

### 4.6 Sealed revision

A sealed revision is Eventic's canonical, immutable internal commit representation.
Its payload has already passed Pydantic validation, storage serialization,
canonicalization, deserialization, and semantic round-trip verification.

## 5. The sealed invariant kernel

The following behavior belongs to Eventic core and is deliberately not extensible:

- Aggregate identity is `(stream, id)`.
- Revision identity and sequence rules.
- Create-versus-update state transitions.
- Optimistic compare-and-swap behavior.
- Command idempotency semantics.
- Schema-version interpretation.
- Pydantic revalidation before sealing a revision.
- Canonical JSON construction.
- The requirement that log, head, returned revision, and emitted event derive from
  the same canonical payload.
- Atomic log, head, and durable-delivery-intent writes.
- The durable delivery state machine and intent identity.
- The definition of transaction success and `committed_at`.
- The rule that a historical revision is never transformed after it has been
  reconstructed under its declared schema evolution chain.

An extension can reject a proposed commit before sealing, observe a commit after it
becomes durable, or consume a sealed value through a constrained protocol. It cannot
replace or wrap the invariant kernel.

## 6. Architectural layers

```text
┌──────────────────────────────────────────────────────────────┐
│ User application                                             │
│ Pydantic models, handlers, policies, extension configuration │
├──────────────────────────────────────────────────────────────┤
│ Declarative catalog                                          │
│ Stream, Subscription, Projection, bindings, Extension output │
├──────────────────────────────────────────────────────────────┤
│ Application compiler                                         │
│ schemas, IDs, ordering, compatibility, migrations, manifests │
├──────────────────────────────────────────────────────────────┤
│ Sealed Eventic kernel                                        │
│ validate → canonicalize → CAS → log/head/outbox → commit      │
├──────────────────────────────────────────────────────────────┤
│ Narrow runtime protocols                                     │
│ Store, Layout, DeliveryDriver, Observer, ProjectionBackend   │
├──────────────────────────────────────────────────────────────┤
│ Infrastructure                                               │
│ PostgreSQL, memory, queues, tracing, search, object storage   │
└──────────────────────────────────────────────────────────────┘
```

Dependencies point downward. Infrastructure implementations may depend on Eventic's
public protocol and data-transfer modules, but core never imports third-party
extensions.

## 7. Pydantic remains the schema engine

User state is a plain Pydantic model:

```python
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from eventic import Indexed, Stream


class Todo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Annotated[str, Field(min_length=1, max_length=200)]
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
```

Each stream owns one cached `TypeAdapter[T]`. The adapter is the public-Pydantic
boundary for:

- Python input validation.
- JSON hydration.
- JSON-safe storage serialization.
- Semantic round-trip verification.
- Validation and serialization JSON Schemas.
- Generic `Revision[T]` and `Committed[T]` envelope schemas.

Plugins must not call Pydantic internals or alter a model's core schema after the
application has compiled. Field-local declarations may use `Annotated` metadata,
which leaves the type seen by static type checkers unchanged.

## 8. Public application model

The minimal explicit application is:

```python
from eventic import Application


application = Application(
    streams=[Todos],
    subscriptions=[],
    policies=[],
    observers=[],
    projections=[],
    extensions=[],
)
```

All inputs are copied into immutable tuples. `Application` is a declaration, not a
service locator and not a runtime singleton.

A store is supplied separately:

```python
store = PostgresStore(DATABASE_URL)
plan = application.compile(store=store, runtime="async")
runtime = AsyncRuntime(plan)
```

The same application can compile against `MemoryStore` in tests and
`PostgresStore` in production, provided both satisfy every capability required by
the catalog.

## 9. Stream-local contributions

Stream-local behaviors are declared directly on `Stream`, not on the Pydantic model:

```python
Todos = Stream(
    Todo,
    name="todos",
    schema_version=2,
    upcasters={
        1: todo_v1_to_v2,
    },
    policies=[
        RequireActor(),
        TenantBoundary(field="organization_id"),
    ],
    normalizers=[
        NormalizeLabels(),
    ],
    projections=[
        Index("status"),
    ],
)
```

The order in each list is execution order. Eventic does not inspect class MRO,
decorator execution order, module import order, or a `priority` attribute.

`Stream` must be immutable after construction. Builder-style helpers may return a
new stream value, but must never mutate a stream already installed in an
application.

## 10. Commit lifecycle

The lifecycle has fixed phases and phase-specific extension points:

```text
1. Resolve command and expected revision
2. Load canonical prior state inside the transaction
3. Validate proposed user state with Pydantic
4. Run deterministic state normalizers in declared order
5. Revalidate the normalized state with Pydantic
6. Evaluate commit policies in declared order
7. Serialize, canonicalize, hydrate, and verify semantic round trip
8. Seal the proposed revision
9. Append log row using optimistic compare-and-swap
10. Derive/update head from that sealed revision
11. Stage every durable delivery intent
12. Commit the database transaction
13. Construct public Revision and Committed envelopes from the sealed revision
14. Run post-commit observers and inline subscriptions
```

Extensions cannot insert phases or change this order.

## 11. Commit policies

A policy inspects a valid proposal and either permits it or raises a typed rejection.
It cannot change the proposal.

```python
from typing import Protocol, TypeVar

StateT = TypeVar("StateT")
MetaT = TypeVar("MetaT")


class CommitPolicy(Protocol[StateT, MetaT]):
    def check(self, proposal: Proposal[StateT, MetaT]) -> None: ...
```

`Proposal` contains read-only values:

```python
class Proposal[StateT, MetaT]:
    stream: Stream[StateT]
    aggregate_id: UUID
    previous: Revision[StateT, MetaT] | None
    proposed: StateT
    metadata: MetaT
    command_id: CommandId | None
```

Policies may be:

- Pure rules using proposal state and typed metadata.
- Transactional rules using an explicitly supplied read-only transaction view.
- Sync or async, but never ambiguously both.

Authorization and uniqueness rules are policies. State shape and field invariants
remain Pydantic validators.

A rejected policy raises `CommitRejected` with a stable machine-readable code:

```python
raise CommitRejected(
    code="todo.actor_required",
    message="An authenticated actor is required",
)
```

## 12. State normalizers

A normalizer is a deterministic, side-effect-free transform:

```python
class StateNormalizer(Protocol[StateT]):
    def normalize(self, value: StateT) -> StateT: ...
```

The output is always passed through the stream's `TypeAdapter` again. A normalizer
cannot directly construct a sealed revision or durable row.

Normalizers should be rare. A transform belonging to one model should normally be a
Pydantic field/model validator. The separate protocol exists for cross-cutting,
reusable normalization whose ordering must be explicit.

Normalizers must not:

- Read clocks, random generators, environment variables, or mutable global state.
- Perform network or database I/O.
- Depend on the current schema version without declaring that dependency.
- Modify the supplied object in place.

## 13. Typed metadata providers

Free-form `dict[str, Any]` metadata is replaced by an application-selected Pydantic
type:

```python
class RequestMetadata(BaseModel):
    actor_id: UUID | None = None
    correlation_id: str
    source: Literal["api", "worker", "import"]


application = Application(
    streams=[Todos],
    metadata=RequestMetadata,
)
```

A metadata provider may supply defaults before metadata validation:

```python
class MetadataProvider(Protocol[MetaT]):
    def provide(self, command: CommandContext) -> Mapping[str, JsonValue]: ...
```

Provider results are merged in declared order, explicit call-site metadata is merged
according to a documented precedence rule, and the result is validated once as
`MetaT`. Providers cannot mutate user state.

Provider output must be deterministic within one command attempt. Correlation IDs,
actor identity, and causation IDs should normally be captured in `CommandContext`
before retryable database work begins.

## 14. Post-commit observers

Observers receive a fully committed, immutable event envelope:

```python
class CommitObserver(Protocol):
    def committed(self, event: Committed[object, object]) -> None: ...
```

Typical observers include:

- Metrics.
- Tracing.
- Structured logging.
- Local cache invalidation where loss is acceptable.
- Test instrumentation.

Observer failure never rolls back a committed transaction. The runtime records and
reports failures according to an explicit observer error policy; it never silently
swallows them.

Observers provide **best-effort process-local observation**, not durable delivery.
Anything that must eventually run is a durable subscription.

Sync and async observers use different protocols or wrappers. Compilation rejects an
async callable in a sync-only runtime rather than invoking it without awaiting it.

## 15. Subscriptions and delivery

Delivery remains a property of the subscription:

```python
application = Application(
    streams=[Todos],
    subscriptions=[
        Subscription(
            id="todo.search-index.v1",
            source=Todos.committed,
            handler=reindex_todo,
            delivery=Outbox(
                queue="search",
                retry=ExponentialBackoff(max_attempts=12),
                dead_letter_queue="search-failed",
            ),
        ),
    ],
)
```

`Todos.committed` is a declarative event source, not a global registry. Constructing
a `Subscription` has no side effects.

Every subscription has a user-supplied, stable ID. Function module and qualified
name may be diagnostic metadata but are not durable identity because refactoring a
function should not accidentally create a new delivery consumer.

### 15.1 Delivery protocol

The high-level delivery policy describes semantics:

```python
class Delivery(Protocol):
    @property
    def requirements(self) -> type[StoreCapability]: ...

    def compile(
        self,
        subscription: Subscription,
        context: CompileContext,
    ) -> CompiledDelivery: ...
```

The compiled driver receives only sealed events or durable references:

```python
class DeliveryDriver(Protocol):
    def stage(
        self,
        transaction: StoreTransaction,
        intent: DeliveryIntent,
    ) -> None: ...
```

Built-in policies may include:

- `Inline()` — post-commit, best effort, same process.
- `Outbox(queue=...)` — intent staged in the commit transaction; at-least-once.
- `Disabled()` — explicitly suppress a route in a deployment or test plan.

Third parties may provide DBOS, Kafka-outbox, SQS, or workflow delivery, but a policy
claiming transactional durability must compile only against a store supporting
atomic delivery-intent staging.

### 15.2 Durable event contract

An outbox row stores stable references and delivery state, not arbitrary pickled
objects or raw Python callables:

```python
class DeliveryIntent(BaseModel):
    intent_id: UUID
    subscription_id: str
    stream: str
    aggregate_id: UUID
    revision: int
    schema_version: int
    queue: str
```

Workers load the revision through the same application plan, reconstruct the typed
`Committed[T, MetaT]` envelope, and invoke the handler. Retry, leases, dead-lettering,
and redrive operate on the intent state machine.

## 16. Store backend protocol

`Store` is an exclusive application resource, not a stream plugin. A conforming store
must uphold the entire core storage contract:

```python
class Store(Protocol):
    def compile(self, manifest: StorageManifest) -> CompiledStore: ...
    def transact(self) -> StoreTransaction: ...
    def get(self, key: RevisionKey) -> StoredRevision | None: ...
    def history(self, aggregate: AggregateKey) -> Iterable[StoredRevision]: ...
```

The real protocol would be expressed in smaller internal read/write transaction
interfaces, but it must guarantee:

- Atomic append, head advance, and durable-intent staging.
- Compare-and-swap on `(stream, aggregate_id, expected_revision)`.
- Exact historical reads.
- Stream-qualified uniqueness.
- UTC commit timestamps assigned at the durable boundary.
- Store-scoped transactions with no ambient cross-store session reuse.
- A migration and schema inspection mechanism.

Backends unable to provide this contract are not Eventic stores. The framework should
not weaken semantics to admit them.

## 17. Store capabilities

Optional features use structural protocols rather than string tokens:

```python
@runtime_checkable
class SupportsTransactionalOutbox(Store, Protocol):
    def stage_delivery_intent(
        self,
        transaction: StoreTransaction,
        intent: DeliveryIntent,
    ) -> None: ...


@runtime_checkable
class SupportsJsonProjections(Store, Protocol):
    def install_projection(self, projection: CompiledProjection) -> None: ...
```

An outbox delivery policy requires `SupportsTransactionalOutbox`; a JSON-path index
projection requires `SupportsJsonProjections`. `Application.compile()` performs the
runtime check while static typing helps extension authors satisfy it.

Capabilities represent actual callable behavior. Marker booleans such as
`json_documents = True` are insufficient.

## 18. Revision layouts

The logical state of every revision is always one full canonical Pydantic document.
A `RevisionLayout` is a physical store strategy:

```python
class RevisionLayout(Protocol):
    def encode(
        self,
        previous: CanonicalPayload | None,
        current: CanonicalPayload,
    ) -> PhysicalPayload: ...

    def reconstruct(
        self,
        rows: Sequence[PhysicalRevisionRow],
    ) -> CanonicalPayload: ...
```

Examples:

```python
Snapshots()
CheckpointedDeltas(every=20)
CompressedSnapshots(algorithm="zstd")
```

Layouts are selected by deployment storage configuration:

```python
store = PostgresStore(
    DATABASE_URL,
    layouts={
        Todos: CheckpointedDeltas(every=20),
        AuditEntries: CompressedSnapshots(algorithm="zstd"),
    },
)
```

A layout may optimize log representation, but:

- It receives an already sealed canonical payload.
- It cannot compute or mutate user state.
- The head is derived from the sealed revision, not separately from caller state.
- Reconstruction must be byte-for-byte canonical.
- Rebuilding heads must use the store/layout abstraction, not hard-coded wire shapes.
- Changing layouts does not alter the stream schema version.

Eventic should publish a layout conformance suite covering exact reads, complete
history, missing checkpoints, corruption, concurrent appends, head rebuilds, and
mixed-layout migrations.

## 19. Payload transforms

Compression or encryption may be modeled as physical payload transforms composed by
the store:

```python
class PayloadTransform(Protocol):
    id: str

    def encode(self, payload: bytes, context: PayloadContext) -> bytes: ...
    def decode(self, payload: bytes, context: PayloadContext) -> bytes: ...
```

Transforms operate only on canonical bytes and must be exactly reversible. Their
stable IDs and configuration versions are stored with the physical row so historical
payloads remain decodable.

Transform order is declaration order on write and reverse order on read:

```python
payload_transforms=[
    Compress("zstd"),
    Encrypt(key_provider=KmsKeyProvider(...)),
]
```

Encryption keys, credentials, and database URLs are runtime resources. They are
never serialized into an application manifest, outbox payload, log row, or process
registry.

## 20. Schema evolution

Evolution is a stream declaration, not an interceptor:

```python
Todos = Stream(
    TodoV2,
    name="todos",
    schema_version=2,
    upcasters={
        1: todo_v1_to_v2,
    },
)
```

An upcaster transforms JSON values from exactly one version to the next:

```python
class Upcaster(Protocol):
    from_version: int
    to_version: int

    def __call__(self, value: JsonObject) -> JsonObject: ...
```

Compilation requires a complete, unambiguous chain from every supported stored
version to the current version. The final JSON value is validated with the current
stream adapter.

Upcasters must be deterministic and side-effect free. They do not receive a store,
clock, network client, or request context.

Pydantic aliases may preserve API input compatibility, but aliases are not durable
schema migrations.

## 21. Projections and indexes

A projection is a declared, rebuildable derivation from sealed revisions:

```python
class Projection[StateT](Protocol):
    id: str

    def compile(
        self,
        stream: Stream[StateT],
        context: CompileContext,
    ) -> CompiledProjection: ...
```

Simple field indexes may be declared through Pydantic `Annotated` metadata:

```python
class Customer(BaseModel):
    organization_id: Annotated[UUID, Indexed()]
    email: Annotated[EmailStr, Indexed(unique=True)]
```

Complex projections remain explicit:

```python
ActiveTodos = Projection(
    id="todo.active.v1",
    source=Todos,
    select=JsonPath("$.status"),
    where=Equals("active"),
)
```

The compiler resolves field paths against the Pydantic schema, records the projection
in the application manifest, and asks the store backend to compile it to its native
representation.

Projection requirements and conflicts are checked before migration generation. For
example, a field cannot be both randomly encrypted and equality-indexed unless the
selected backend/extension provides an explicit compatible scheme.

## 22. Extension bundles

An extension packages related declarations:

```python
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Contributions:
    streams: tuple[Stream, ...] = ()
    policies: tuple[PolicyBinding, ...] = ()
    normalizers: tuple[NormalizerBinding, ...] = ()
    metadata_providers: tuple[MetadataProvider, ...] = ()
    observers: tuple[ObserverBinding, ...] = ()
    subscriptions: tuple[Subscription, ...] = ()
    projections: tuple[Projection, ...] = ()
    schema_fragments: tuple[SchemaFragment, ...] = ()


class Extension(Protocol):
    id: str

    def contributions(
        self,
        catalog: ReadOnlyCatalog,
    ) -> Contributions: ...
```

The supplied catalog is read-only and contains declarations, not a connected store or
runtime. Expansion must be deterministic.

Extensions cannot contribute arbitrary callbacks into unnamed phases. Every returned
object must satisfy one of the known contribution types.

### 22.1 Why bundles are not a second extension system

After expansion, the compiler treats bundled and directly declared contributions
identically. The extension object is not retained in the runtime plan unless it also
appears explicitly as a component contribution.

This allows convenient third-party packaging without creating two ways to execute
runtime behavior.

## 23. Complete third-party extension example

Consider an `eventic-search` package that maintains an external search index.

```python
from dataclasses import dataclass

from eventic import Contributions, Outbox, Projection, Subscription


@dataclass(frozen=True)
class SearchExtension[StateT]:
    stream: Stream[StateT]
    index: str
    queue: str = "search"

    @property
    def id(self) -> str:
        return f"search:{self.stream.name}:{self.index}"

    def contributions(self, catalog: ReadOnlyCatalog) -> Contributions:
        projection = Projection(
            id=f"{self.stream.name}.search-document.v1",
            source=self.stream,
            project=self._project,
        )
        subscription = Subscription(
            id=f"{self.stream.name}.search-delivery.v1",
            source=self.stream.committed,
            handler=self._index,
            delivery=Outbox(queue=self.queue),
        )
        return Contributions(
            projections=(projection,),
            subscriptions=(subscription,),
        )

    def _project(self, revision: Revision[StateT]) -> SearchDocument:
        ...

    async def _index(self, event: Committed[StateT]) -> None:
        ...
```

Application usage is explicit:

```python
application = Application(
    streams=[Todos],
    extensions=[
        SearchExtension(
            stream=Todos,
            index="todos-v2",
            queue="search",
        ),
    ],
)
```

Compilation verifies:

- The extension ID is unique.
- Both contribution IDs are unique.
- `Todos` is installed in the application.
- The projection accepts `Revision[Todo]`.
- The async handler is compatible with the selected worker runtime.
- The selected store supports transactional outbox staging.
- The queue has a configured worker/driver.
- The handler's event schema is included in the application manifest.

No decorator registry or module scanning is involved.

## 24. Application compilation

Compilation is a pure planning phase except for optional read-only store capability
inspection. It performs these steps:

1. Normalize direct application declarations.
2. Assign source locations for diagnostics.
3. Check unique extension IDs.
4. Expand extensions in declared order.
5. Reject contribution types unknown to core.
6. Check unique stream and contribution IDs.
7. Resolve stream references to installed streams.
8. Build and cache Pydantic adapters.
9. Generate validation, storage, revision, metadata, command, and event schemas.
10. Compute schema fingerprints.
11. Validate upcaster chains.
12. Resolve field-level `Annotated` declarations.
13. Resolve component ordering.
14. Check sync/async callable compatibility.
15. Check store capability requirements.
16. Check delivery-driver and queue bindings.
17. Compile projections and physical layouts.
18. Generate the expected storage/migration manifest.
19. Compare the expected manifest with installed storage when requested.
20. Freeze and return `ApplicationPlan`.

Compilation collects independent errors and reports them together where possible:

```text
Application compilation failed with 3 errors:

  subscriptions[todo.search.v1]
    requires SupportsTransactionalOutbox, but MemoryStore does not provide it

  streams[todos].upcasters
    missing transition 2 -> 3

  projections[todo.email.v1]
    field "email" is randomly encrypted and cannot use an equality index
```

## 25. Identity, conflicts, and ordering

Every declaration that can persist data or affect durable behavior has an explicit,
stable ID:

- Stream name.
- Extension ID.
- Subscription ID.
- Projection ID.
- Layout/transform ID and version.
- Handler logical ID through its subscription.

Python qualified names are diagnostic, not identity.

Duplicate IDs are always errors. Eventic never uses first-wins or last-wins
registration.

Stacking component order is the order written by the application after extension
expansion. Extensions must preserve their own local contribution order. The framework
does not provide numeric priorities.

If an extension needs a relationship such as “tracing wraps every observer,” it must
provide one composite observer or require the application to place components
explicitly. Hidden cross-extension reordering is not allowed.

Exclusive resources are selected through singular constructor arguments or mappings,
making conflicts structurally difficult to express:

```python
PostgresStore(
    layout=Snapshots(),
)
```

or, per stream:

```python
PostgresStore(
    layouts={Todos: CheckpointedDeltas(every=20)},
)
```

## 26. Schema and migration contributions

Extensions may contribute backend-neutral schema requirements, not arbitrary SQL in
the core catalog:

```python
class SchemaFragment(Protocol):
    id: str

    def compile(self, backend: SchemaBackend) -> MigrationOperations: ...
```

Examples include:

- A projection index.
- A delivery-intent table/column requirement.
- A lease index for a worker driver.
- A backend-supported generated column.

Backend-specific packages may emit native migration operations, but those operations
must appear in the compiled storage manifest and participate in `schema check`.

Migrations are shipped inside the owning wheel. Extension migration IDs are
namespaced by extension ID, and dependency ordering is explicit.

An extension may not execute DDL at import or application construction time.

## 27. Runtime resources and dependency injection

Extensions receive concrete clients through ordinary constructors:

```python
telemetry = OpenTelemetryObserver(tracer=tracer)
search = SearchExtension(stream=Todos, client=search_client, index="todos-v2")
```

Eventic does not resolve arbitrary constructor dependencies. This keeps ownership,
lifetime, secrets, and test replacement visible to application code.

Long-lived resources with startup/shutdown behavior implement a narrow lifecycle
protocol:

```python
class RuntimeResource(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
```

The compiled plan owns only resources explicitly contributed to it. Shutdown occurs
in reverse startup order. A failed startup stops already-started resources before
propagating the error.

## 28. Sync and async model

Eventic should expose distinct sync and async runtime surfaces:

```python
SyncCommitPolicy
AsyncCommitPolicy
SyncCommitObserver
AsyncCommitObserver
Store
AsyncStore
Runtime
AsyncRuntime
```

Where implementation reuse is practical, it remains internal. User-facing protocols
must make awaiting explicit.

Compilation rejects:

- Coroutine functions installed in sync-only phases.
- Sync handlers installed where a driver requires async cancellation semantics,
  unless wrapped in an explicit executor adapter.
- Objects that return awaitables despite declaring a sync protocol.

An explicit adapter may move a synchronous handler to a configured executor:

```python
handler=InThreadPool(reindex_sync, executor=worker_pool)
```

There is no automatic coroutine detection at delivery time.

## 29. Error taxonomy

Extension-related failures should be precise:

```text
ApplicationCompileError
├── DuplicateComponentId
├── UnknownContribution
├── MissingStream
├── ComponentTypeMismatch
├── UnsupportedStoreCapability
├── InvalidComponentOrder
├── IncompleteSchemaEvolution
├── SchemaFingerprintMismatch
├── ProjectionConflict
├── DeliveryConfigurationError
└── RuntimeModeMismatch

ExtensionRuntimeError
├── PolicyFailure
├── NormalizerFailure
├── ObserverFailure
├── ProjectionFailure
├── DeliveryFailure
└── PayloadTransformFailure
```

Pydantic state validation failures remain ordinary `pydantic.ValidationError` values.
Eventic should not wrap them unless it preserves the original error as the direct
cause and exposes it intact.

All compile errors include the component ID, declaration source when available, and
the exact unmet protocol or schema rule.

## 30. Introspection and operability

The application plan is inspectable:

```python
print(plan.describe())
print(plan.describe_stream(Todos))
print(plan.schema_manifest())
print(plan.storage_manifest())
```

CLI examples:

```console
eventic --app myapp.eventic:application inspect
eventic --app myapp.eventic:application inspect --stream todos
eventic --app myapp.eventic:application schema export
eventic --app myapp.eventic:application schema check
eventic --app myapp.eventic:application schema diff
eventic --app myapp.eventic:application migrate
eventic --app myapp.eventic:application worker --queue search
eventic --app myapp.eventic:application deliveries dead-letter list
eventic --app myapp.eventic:application deliveries redrive --subscription todo.search-index.v1
```

An `inspect --stream todos` report should show the resolved pipeline:

```text
stream: todos
state: myapp.models.Todo
schema version: 2
schema fingerprint: sha256:...
normalization: Pydantic[Todo] -> NormalizeLabels -> Pydantic[Todo]
policies: RequireActor -> TenantBoundary
layout: CheckpointedDeltas(every=20)
payload transforms: Compress(zstd) -> Encrypt(kms-v2)
projections: todo.status.v1
subscriptions:
  - todo.search-index.v1 -> outbox[search]
  - todo.metrics.v1 -> inline
required store capabilities:
  - SupportsTransactionalOutbox
  - SupportsJsonProjections
```

No behavior relevant to a commit should be absent from this report.

## 31. Packaging and discovery

A third-party package exports ordinary Python symbols:

```python
from eventic_search import SearchExtension
```

The package may advertise itself in documentation or package metadata, but Eventic
does not automatically import installed entry points. Automatic discovery creates
non-local behavior, startup nondeterminism, security exposure, and uninspectable test
environments.

Recommended package structure:

```text
eventic_search/
├── __init__.py
├── extension.py
├── projection.py
├── delivery.py
├── schemas.py
├── migrations/
└── py.typed
```

Plugin packages declare compatible Eventic and Pydantic version ranges and ship a
`py.typed` marker.

## 32. Conformance testing

Eventic should publish reusable test suites rather than relying only on protocols.
Structural typing proves shape, not behavior.

### 32.1 Store conformance

- Atomic log/head/intent commit.
- Rollback at every write boundary.
- Same-revision replay versus semantic conflict.
- Cross-stream IDs.
- Concurrent compare-and-swap.
- Exact reads and complete history.
- Head rebuild equivalence.
- Transaction scope isolation.
- UTC timestamps.
- Migration upgrade/downgrade safety.

### 32.2 Layout conformance

- Canonical byte reconstruction.
- Long histories.
- Corrupt/missing base rows.
- Checkpoint boundaries.
- Layout-version upgrades.
- Mixed historical layouts.

### 32.3 Delivery conformance

- Atomic staging.
- Lease ownership and expiry.
- At-least-once retry behavior.
- Stable intent identity.
- Duplicate worker attempts.
- Dead-letter transition.
- Redrive.
- Handler schema reconstruction after process restart.

### 32.4 Transform conformance

- Exact encode/decode round trip.
- Wrong key/version behavior.
- Corrupt payload behavior.
- Historical configuration lookup.
- No secret leakage in errors or manifests.

### 32.5 Extension conformance

- Deterministic contribution expansion.
- Stable IDs.
- No global mutation during import or expansion.
- Complete schema declarations.
- Sync/async correctness.
- Helpful compile failures for missing capabilities.

Each external package should be able to run these suites against its implementation.

## 33. Security boundaries

Extensions run application code and are therefore trusted code, but the framework
still limits accidental authority:

- Compilation receives no database credentials unless a store capability inspection
  explicitly requires a connected store.
- Read-only catalogs expose declarations, not live mutable runtime objects.
- Policies receive the minimum transaction view required for their contract.
- Observers receive immutable committed envelopes.
- Delivery handlers receive reconstructed typed events, not arbitrary ORM sessions.
- Manifests redact resource configuration and never serialize secrets.
- Payload-transform errors identify transform IDs without printing payloads or keys.
- Extension packages are never imported merely because they are installed.

The CLI should display the module path and distribution version of every installed
extension contribution so operators can audit the runtime plan.

## 34. Versioning and compatibility

There are three distinct compatibility surfaces:

1. **Eventic protocol compatibility** — whether a plugin implements the current
   runtime contracts.
2. **Application schema compatibility** — whether stored stream versions have valid
   upcaster chains and expected fingerprints.
3. **Physical format compatibility** — whether stores, layouts, and transforms can
   decode historical rows.

These versions must not be conflated.

Every compiled manifest records:

- Eventic library version.
- Pydantic version.
- Extension distribution names and versions.
- Stream schema versions and fingerprints.
- Layout IDs and versions.
- Payload-transform IDs and versions.
- Projection IDs and versions.
- Subscription IDs.

Changing a Python implementation without changing durable behavior need not create a
new stream schema version. Changing durable JSON shape, layout encoding, transform
configuration, projection definition, or subscription identity must follow the
versioning rules for that specific surface.

## 35. Mapping from current concepts

| Current concept | Rewrite destination | Decision |
|---|---|---|
| `RowStore` | `Store` plus transaction protocols | Retain, strengthen behavioral contract |
| `JsonRowStore` marker | `SupportsJsonProjections` or another callable protocol | Replace marker attribute with real capability |
| `Snapshot` codec | `Snapshots` revision layout | Retain as physical default |
| `Delta` codec | `CheckpointedDeltas` revision layout | Retain without logical-state authority |
| `Interceptor.before_commit` veto | `CommitPolicy` | Retain as read-only decision |
| `Interceptor.before_commit` enrichment | Pydantic validator, `StateNormalizer`, or typed metadata provider | Split by semantics and revalidate |
| `Interceptor.after_commit` | `CommitObserver` or `Inline` subscription | Retain with explicit reliability contract |
| `Interceptor.after_hydrate` | Upcaster, payload transform, or presentation model | Remove generic hook |
| `on_commit` decorator | Explicit `Subscription` value | Remove registration side effect |
| Handler function identity | Explicit subscription ID | Replace refactor-sensitive identity |
| Outbox mode | `Outbox` delivery policy | Retain and formalize state machine |
| DBOS integration | Third-party `Delivery`/worker adapter | Isolate from core |
| Stream class keyword | `Stream(Todo, ...)` constructor | Replace model inheritance/configuration |
| `provides`/`requires` strings | Python protocols checked at compilation | Retain type-system principle |
| Generic `Plugin` base | Optional declarative `Extension` bundle | No privileged plugin superclass |
| Process-global registries | Immutable `ApplicationPlan` | Remove |

## 36. Representative end-state

```python
class Todo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: Annotated[UUID, Indexed()]
    text: Annotated[str, Field(min_length=1)]
    status: Literal["backlog", "active", "complete"] = "backlog"


Todos = Stream(
    Todo,
    name="todos",
    schema_version=2,
    upcasters={1: todo_v1_to_v2},
    policies=[TenantBoundary(field="organization_id")],
)

application = Application(
    streams=[Todos],
    metadata=RequestMetadata,
    observers=[OpenTelemetryObserver(tracer)],
    subscriptions=[
        Subscription(
            id="todo.audit.v1",
            source=Todos.committed,
            handler=write_audit_record,
            delivery=Outbox(queue="audit"),
        ),
    ],
    extensions=[
        SearchExtension(
            stream=Todos,
            client=search_client,
            index="todos-v2",
        ),
    ],
)

store = PostgresStore(
    DATABASE_URL,
    layouts={Todos: CheckpointedDeltas(every=20)},
    payload_transforms=[Compress("zstd")],
)

plan = application.compile(store=store, runtime="async")
runtime = AsyncRuntime(plan)
```

Application code remains straightforward:

```python
todos = runtime.collection(Todos)

todo = await todos.create(
    Todo(
        organization_id=organization_id,
        text="Learn Eventic",
    ),
    metadata=RequestMetadata(
        actor_id=current_user.id,
        correlation_id=request_id,
        source="api",
    ),
)

todo = await todos.change(todo, status="complete")
```

The user sees a small Pydantic-native document API. Extension authors see narrow,
typed contracts. Operators see one compiled and inspectable plan. Eventic core retains
exclusive ownership of the invariants that make every revision trustworthy.

## 37. Final design rule

The framework should use this test for every proposed extension point:

> Can this behavior be added without allowing two parts of the system to disagree
> about what was committed?

If yes, expose the smallest protocol that expresses it. If no, keep it inside the
sealed kernel.
