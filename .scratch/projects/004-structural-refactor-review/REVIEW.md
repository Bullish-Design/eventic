# Eventic 0.3 — Adversarial Structural Refactor Review

Date: 2026-08-05
Scope: architecture, conceptual integrity, public API, persistence, concurrency,
delivery, migrations, packaging, typing, tests, security, performance, and
operability
Baseline: `main` at `d39e9e6` (`origin/main` matched; worktree initially clean)

## Verdict

Eventic's central idea is good: an explicitly committed Pydantic aggregate, an
append-only version log, and a transactionally maintained head/outbox can form a
small and compelling library. The 0.3 rewrite also removed several genuinely bad
0.2 mechanisms. The module graph is compact, construction is pure, ordinary
same-version races are loud, and the owned-session post-commit boundary is much
cleaner than the previous design.

The implementation is nevertheless **not release-ready**. The strongest claims in
the concept document are not merely under-tested; several are false under normal,
supported Python and Pydantic behavior:

- A frozen `Record` is only shallowly frozen. Mutating a nested list after `save()`
  can make `get(id)` disagree with `get(id, version=N)` and `history(id)`.
- `update()` accepts an unsaved or fabricated version and can create a delta stream
  that latest reads can see but historical reads cannot reconstruct.
- Two different streams using the same UUID and state cause the second save to be
  silently discarded as an “idempotent replay.”
- The Alembic-created SQLite outbox cannot insert rows because its primary key is
  `BIGINT`, while the working `create_tables()` schema deliberately uses an
  `INTEGER` variant.
- The wheel contains no Alembic migrations even though production users are told
  that Alembic is the source of truth and to run `alembic upgrade head`.
- Standard Pydantic `computed_field` output is persisted as input and makes the
  record impossible to hydrate.
- A nested unit of work opened for Store B while Store A has an active transaction
  silently writes B's operation into A's database.

Those are release blockers because they permit silent loss, divergent sources of
truth, unreconstructable history, wrong-database writes, or a production path that
does not work. The right response is not a collection of local patches. The commit
path needs one canonical state, one store-scoped transaction, one aggregate key,
and one projection path.

## What is already strong

The review should preserve these decisions:

1. **Pure construction and explicit persistence.** `Record(...)` does no I/O and
   `save` / `update` / `draft().commit()` are visible write boundaries.
2. **A small public API.** The post-0.2 surface is much easier to reason about.
3. **Append rather than overwrite.** The log/head split is the correct shape once
   the head is truly derived from the log.
4. **Post-commit dispatch owned by the transaction boundary.** The core tests prove
   rollback suppression for owned and foreign sessions.
5. **Loud ordinary stale writes.** Two writers targeting the same next version with
   different state do not silently overwrite each other.
6. **Outbox references rather than pickled records.** Rehydrating a version is a
   much better durable payload contract than serializing application objects.
7. **Class keywords instead of Pydantic mixins.** This avoids the prior phantom
   field and MRO failures.
8. **Errors share a library base type.** The error surface is small and usable.

## Severity model

- **Blocker** — can lose data, make authoritative views disagree, write to the
  wrong database, prevent a documented production path, or make stored history
  unreconstructable.
- **High** — violates a public architectural contract, breaks durable delivery or
  schema evolution, leaks credentials, or creates a persistent operational trap.
- **Medium** — produces inconsistent semantics, weak extension contracts,
  scalability problems, or substantial maintenance risk.
- **Low** — local hygiene or documentation debt with limited immediate impact.

## Invariant audit

| Invariant | Result | Evidence |
|---|---|---|
| I1 Append-only / immutable committed versions | **Fail** | Database inserts are append-only through `SqlStore`, but records are deeply mutable and the head can diverge from log replay (F01). The database itself has no immutability constraint. |
| I2 No hidden writes | **Pass, narrow** | Public record writes are explicit. The exposed SQLAlchemy session and internal ORM models can still mutate the log, but no record attribute hook performs I/O. |
| I3 Pure construction | **Pass** | Construction succeeds without an active store and does no I/O. |
| I4 Deterministic identity | **Partial** | The function is deterministic, but omitting `stream` conflates different aggregates and enables silent cross-stream loss (F03). |
| I5 Loud conflicts / byte-identical replay only | **Fail** | Replay compares only `version_id` and `data`; it ignores stream, kind, codec shape, and snapshot status. Cross-stream semantic conflicts can be silent (F03). |
| I6 Core imports only Pydantic + SQLAlchemy | **Partial / misleading** | The import-graph test passes, but the installed core declares four mandatory dependencies, including Alembic and `python-dotenv` (F23). |
| I7 One event after durability | **Partial** | Rollback timing works, but inline and durable events differ, async handlers silently do not run, and “exactly once” is impossible for non-durable inline callbacks across a process crash (F10/F14/F19). |
| I8 No mutable process state outside a Store | **Fail as written** | `_CURRENT` is operational context state and DBOS `_QUEUES` is a mutable runtime registry. Store activation also keeps its context token on the shared Store instance (F08/F16/F20). |

The broader claim that `head` and `outbox` are “derived and rebuildable” also
fails. The head rebuilder only understands the built-in wire shapes, retains
orphan rows, and the outbox cannot be reconstructed without inventing redelivery
semantics (F11/F14).

---

## Release blockers

### F01 — The log and head can represent different state

**Locations:** `record.py:33-39`, `state.py:19-21`, `codec/delta.py:33-44`,
`pipeline.py:40-70`

Pydantic's `frozen=True` prevents field assignment; it does not recursively freeze
lists, dicts, nested models, or arbitrary user objects. Eventic then trusts the
caller's `prev` object when constructing a delta but writes the head independently
from `user_state(new)`.

Reproduced sequence:

```python
base = Mutable(tags=["persisted"]).save()
base.tags.append("in-memory-only")
base.update(n=1)
```

Observed:

- latest `get(id)` from the head: `['persisted', 'in-memory-only']`
- exact `get(id, version=1)` from the log: `['persisted']`
- `history(id)[-1]` from the log: `['persisted']`

The head is therefore not a projection of the log. Rebuilding heads changes
observable application state. This defeats the foundational “log is truth” claim.

The root defect is broader than shallow freezing: the pipeline creates two
representations through two independent computations. A custom or buggy codec can
produce the same divergence even if values are deeply immutable.

**Required direction:** load the canonical previous state from the active
transaction, encode the canonical proposed state once, and derive the next head by
applying/decoding that encoded row. Never derive the log from one value and the
head from another. Either enforce recursively immutable supported field types or
state honestly that records are assignment-frozen and defensively copy all state.
In either case, never trust a caller-held object as proof of persisted prior state.

### F02 — `update()` can create gaps and unreconstructable streams

**Locations:** `record.py:35-38,60-70,83-97`, `pipeline.py:27-70`,
`store/sql.py:50-72`

`version` is accepted from public input, and `update()` does not prove that its
base value was ever saved or is the current durable head. Persistence only checks
whether the target `(id, version)` already exists.

The probe called `Unsaved(required="present").update(n=1)` under `Delta(k=20)`.
It inserted version 1 without version 0. The head could answer the latest read,
while:

- exact version 1 raised `RecordNotFound` because no snapshot existed;
- history raised `ValidationError` because the first delta did not contain the
  required field.

Public callers can also fabricate arbitrary positive or negative versions through
construction or Pydantic's `model_copy(update=...)` and create gaps or a version-0
row whose kind is `update`.

**Required direction:** make managed commit metadata non-input state. A write must
carry `expected_version`; inside the transaction, compare it to the durable head
and require create=`no head + version 0`, update=`existing head + exactly one`.
Compute changes from the persisted head to the validated proposal. Reject detached
or fabricated bases with a dedicated usage/concurrency error.

### F03 — Cross-stream saves can be silently discarded

**Locations:** `identity.py:13-18`, `schema.py:57-70`, `sql.py:50-72`

`version_id` omits the stream, the unique constraint is `(id, version)`, the replay
lookup omits stream, and `_decide` compares only `version_id` and `data`. If two
record classes intentionally use the same UUID and happen to have equal user
state, saving the second is treated as a replay of the first.

The probe saved equal values to `review004_alpha` and `review004_beta` with one
UUID. Only the Alpha row remained; Beta's `save()` returned normally and
`Beta.get(id)` failed.

Even if UUIDs are intended to be globally unique, silent success is unacceptable:
the operations are not semantically identical.

**Required direction:** decide and document the aggregate key. The natural key is
`(stream, id)`, making uniqueness `(stream, id, version)` and version identity a
function of all three. If IDs are intentionally global, compare and reject every
semantic field on collision, including stream, kind, snapshot/codec identity, and
canonical payload.

### F04 — The production Alembic schema breaks SQLite durable delivery

**Locations:** `migrations/versions/0300_triad.py:91-117`,
`store/schema.py:80-119`

The ORM schema correctly uses
`BigInteger().with_variant(Integer(), "sqlite")` for the autoincrementing outbox
primary key. The migration uses unconditional `BigInteger`. SQLite only provides
the required implicit rowid behavior for an `INTEGER PRIMARY KEY`, so saving a
record with an outbox subscription against an Alembic-upgraded SQLite database
raises `IntegrityError`.

The same migration omits `ix_eventic_head_state`. `alembic check` reports both a
missing index and an outbox type difference. This means the two supported schema
creation paths do not describe the same product.

**Required direction:** repair the migration with the dialect variant and missing
index, add `alembic check` as a gate, and run every persistence/delivery test
against both `create_all` and `alembic upgrade head` schemas on SQLite and
Postgres. Production tests must never substitute `create_tables=True` for the
documented migration path.

### F05 — Installed wheels do not contain the migrations

**Locations:** `pyproject.toml:38-39`, `MIGRATION.md:47-51`

The wheel build target packages only `src/eventic`. The built wheel contains the
Python package but not `alembic.ini`, `migrations/env.py`, or any revision. Yet
`Store` defaults to `create_tables=False`, documentation calls Alembic the source
of truth, and migration guidance tells installed users to run `alembic upgrade
head` without explaining how to obtain a script location.

The source distribution has the opposite problem: it includes all historical
`.scratch` reviews and probes, all tests, `uv.lock`, and project-local runtime
configuration.

**Required direction:** package revisions under a stable resource path such as
`eventic/migrations`, expose `eventic migrate` or a documented Alembic integration
helper, and test a clean wheel installation in an empty environment. Define sdist
inclusions explicitly.

### F06 — Standard Pydantic computed fields poison persisted state

**Locations:** `state.py:19-21`, `record.py:87,130,145`

`model_dump()` includes computed fields by default. `user_state()` persists them,
but hydration passes them back as input to a model with `extra="forbid"`.
`@computed_field` is ordinary Pydantic functionality, so the one-sentence thesis
that a plain Pydantic model becomes a Record does not hold.

The probe persisted `{value: 2, doubled: 4}` and then failed to hydrate because
`doubled` is not an input field. Update and Draft dumps are affected as well.

**Required direction:** define a precise persisted-field contract and serialize
only declared input fields (`exclude_computed_fields=True` at minimum). Add a
round-trip property test across computed fields, aliases, serializers, nested
models, enums, UUIDs, datetimes, decimals, secrets, and custom Pydantic types.

### F07 — Nested units of work are not scoped to a Store

**Locations:** `store/__init__.py:51-60`, `store/unit_of_work.py:32-47,87-104`

`UnitOfWork.current()` stores no owner Store. Any active UoW causes every Store to
return `_Nested(cur)`. The probe opened a transaction on Store A, activated Store
B, and saved a record. The record was committed to A; B remained empty.

This is a wrong-database write with no error. It also invalidates the claim that
multiple stores safely coexist.

**Required direction:** a UoW must carry its owning Store/repository identity.
Only same-store nesting may share a session. Cross-store nesting should either
open an independent transaction explicitly or fail loudly; it must never borrow
another Store's session.

---

## High-severity findings

### F08 — The interceptor contract can persist invalid data and returns the wrong value

**Locations:** `pipeline.py:27-40`, `record.py:75-97`, `interceptors.py:17-29`

`before_commit` is a transformer, but its output is not revalidated. The documented
example style uses Pydantic `model_copy(update=...)`, whose updates are not
validated. The probe transformed an `int` field into a string; Eventic warned but
persisted it into the append-only log, after which hydration failed.

The pipeline also returns `None`, so `save()` and `update()` return the object from
before interception. An enrichment can be durably applied while the caller
receives a value that was never committed. The existing F11 regression checks only
that the transformed value appears on a later read, masking this split.

**Required direction:** the commit pipeline must return the committed value. Any
hook that returns a domain record must be revalidated, must preserve class/id/
expected version, and must produce the canonical state used by log, head, event,
and return value. Consider separating domain validation hooks from storage
transformations; encryption/compression belongs in a codec, not in an invalid
Record instance.

### F09 — Durable update deltas use raw, pre-validation Python input

**Locations:** `record.py:83-97`, `pipeline.py:27-70`, `sql.py:96-110`

`changes=dict(changes)` becomes `Event.delta` and then an outbox JSON column.
Datetimes, UUIDs, nested models, and other values accepted by Pydantic can be
non-JSON-native. Updating a datetime field with an outbox subscription raised
`StatementError` and rolled back the entire version. Coercions are also invisible:
an input string validated into an integer remains a string in `delta`.

Interceptor-derived changes are omitted entirely.

**Required direction:** compute the event delta from canonical JSON before/after
states after validation and interception. Do not persist caller kwargs.

### F10 — Inline and durable handlers do not receive equivalent Events

**Locations:** `pipeline.py:44-70`, `unit_of_work.py:70-81`,
`dispatch/outbox.py:84-93`

The inline event holds the pre-hydration `new` record, whose `created_ts` is `None`.
The durable dispatcher rehydrates from the row and receives the actual commit
timestamp. The probe observed exactly that split. Raw deltas create further type
differences after a JSON round trip.

The package says durable handlers receive the same Event that inline handlers get;
they do not receive the same committed value semantics.

**Required direction:** materialize one committed event envelope after the row has
its commit metadata. Inline dispatch and durable reconstruction must be tested for
field-for-field equivalence.

### F11 — `rebuild-heads` is neither codec-agnostic nor an exact rebuild

**Locations:** `pipeline.py:149-197`, `cli.py:25-29`, `seams.py:80-95`

The codec is advertised as an open Protocol, but `rebuild_heads` hardcodes exactly
the Snapshot and Delta wire formats. A valid custom wrapped-snapshot codec rebuilt
its head as `{payload: ...}` and made hydration fail.

The command upserts rows but never removes orphan heads. Therefore it cannot
reproduce the exact projection of the log even for built-in codecs.

**Required direction:** either close the codec set and version the known wire
formats, or store a codec/encoding identifier and dispatch reconstruction through
the correct codec/upcaster. Rebuild into a new projection table or delete the
selected projection scope transactionally before recreating it. Add an equality
check between rebuilt and live projections.

### F12 — The event log has no schema or codec evolution model

**Locations:** `schema.py:53-77`, `pipeline.py:77-119`, `config.py:62-86`

Rows contain stream, kind, snapshot flag, and opaque JSON, but no model schema
version or codec/encoding version. Historical state is always validated against
today's class. Changing required fields, renaming/removing fields, changing custom
serializers, or switching from Delta to Snapshot can make old history unreadable.

For an event-history library this is a central requirement, not a future nicety.
The global stream registry makes the latest class the only possible decoder.

**Required direction:** put `schema_version` and `encoding` on every log row;
define deterministic upcasters from stored state to the current model; retain
decoders for old encodings; and test rolling upgrades. Stream ownership should
include an explicit evolution policy.

### F13 — The standalone drain CLI cannot discover application declarations

**Locations:** `cli.py:32-35`, `config.py:23-59`, `subscribe.py:22-63`,
`dispatch/outbox.py:84-93`

Outbox delivery depends on process-local `_STREAMS` and `_HANDLERS`. The CLI imports
only Eventic; it has no application module/config entry point to load declarations.
The probe launched the documented CLI in a fresh process. It exited 0, printed
`drained 1 outbox rows`, retained the row, incremented attempts, and logged that no
Record class was registered.

The return value is also misleading: `drain()` returns claimed rows, not successful
deliveries.

**Required direction:** require an application bootstrap/import target, or use
installable entry points to build an explicit catalog. Return structured counts
(`claimed`, `succeeded`, `failed`, `dead_lettered`) and make the CLI exit nonzero
when delivery cannot be configured.

### F14 — Durable delivery identity and liveness are underspecified

**Locations:** `subscribe.py:38-63`, `schema.py:96-119`,
`dispatch/outbox.py:45-81`

`(version_id, handler_id)` is the unique delivery key, but a function can legally
have multiple matching subscriptions (different queues or overlapping kinds).
The probe registered one function for `*` and `create`; the duplicate outbox rows
violated the unique constraint and aborted the application write.

Renaming/removing a handler or failing to import its module leaves a row retrying
forever. There is no maximum attempt count, dead-letter state, diagnostic status,
lease metadata, or handler alias/versioning mechanism. `RecordNotFound` is silently
deleted even though log pruning contradicts I1.

**Required direction:** give each subscription an explicit stable ID and reject
duplicate matching semantics at registration. Add a durable state machine with
leases, error summaries, bounded retry/dead-letter policy, redrive, and handler ID
migrations.

### F15 — DBOS payloads persist the raw database URL, including credentials

**Locations:** `contrib/dbos.py:65-85,89-100`, `store/__init__.py:44-46`

`DbosDispatcher` puts `store.url` into every queued payload. `Store.url` retains
the caller's raw URL, so a Postgres password can be serialized into DBOS workflow
inputs and observability/storage surfaces. This is unnecessary credential
replication.

The DBOS adapter also keeps a module-global `_QUEUES` cache whose first
`concurrency` configuration wins silently, contradicting I8's stated operational
state rule.

**Required direction:** pass a non-secret Store/configuration name and resolve
credentials in the worker environment. Own queue definitions in one explicit
application catalog and reject conflicting redeclarations.

### F16 — Store context management is not reentrant or safe for sharing

**Locations:** `store/__init__.py:68-82`

`activate()` stores one ContextVar token on `self._token`. Re-entering the same
Store overwrites the first token; the outer exit then resets an already-used token
and raises `RuntimeError`. Sharing one Store across threads/tasks can similarly
clobber tokens or attempt to reset a token from the wrong context.

The existing concurrency test creates a separate Store per thread, so it does not
test the natural shared-engine use case.

**Required direction:** keep tokens local to each context-manager invocation (or a
per-context stack), make activation return a dedicated binding object, and test
same-instance nesting plus thread/task sharing.

### F17 — Reads do not participate in the current unit of work

**Locations:** `store/__init__.py:55-66`, `pipeline.py:97-143`

Writes nested under `store.unit_of_work()` use its session; reads always open a new
session. A read after a save in the same transaction raises `RecordNotFound` until
the outer commit. This is surprising for a public transaction boundary and makes
atomic multi-step application logic difficult.

**Required direction:** same-store reads should reuse the current UoW session, or
the API must be made internal and explicitly documented as write batching without
read-your-writes semantics.

### F18 — Migration downgrade corrupts Delta representation

**Locations:** `migrations/versions/0300_triad.py:198-252`, `MIGRATION.md:70-71`

The 0.3 downgrade copies each current log row's data dict directly into the old
`records.data` shape. A Delta row becomes `{set, del, id, version, version_id}`;
the old DiffStorage contract requires `{kind: "delta", patch: ...}`. Snapshot and
delta histories are not reconstructed to their former encoding.

The test checks only that three rows survive, not that 0.2 can read them. The
documentation calls the downgrade honest and possible, but it is not semantically
usable for Delta streams.

**Required direction:** implement a genuine reverse encoder per old codec or
declare downgrade unsupported and fail before destructive DDL. Test readability
through the previous released library, not row counts.

### F19 — Coroutine handlers are accepted and silently never awaited

**Locations:** `subscribe.py:38-63`, `dispatch/inline.py:34-44`,
`dispatch/outbox.py:84-93`

Registration accepts any callable. Calling an `async def` handler returns a
coroutine, which the dispatchers ignore; the handler does not run and may only
produce a delayed `RuntimeWarning`. Async interceptor methods have similarly
undefined behavior.

**Required direction:** either support awaitables end-to-end with explicit async
dispatch APIs or reject coroutine functions at declaration time. Validate handler
signature and Record classes eagerly.

### F20 — “Exactly once” is the wrong delivery contract

**Locations:** `README.md:13-19,46-53`, `unit_of_work.py:1-18`,
`dispatch/outbox.py:49-81`

The transaction stages exactly one in-memory Event per inserted version, but an
inline callback can be lost if the process dies after database commit and before
`_flush`. Outbox handlers are explicitly at-least-once because a side effect can
succeed before row deletion commits. Multiple writes can also share one outer
database commit and produce multiple events.

**Required direction:** use precise terms:

- one **event intent** per newly appended version;
- inline handlers are best-effort, post-commit, in-process;
- outbox handlers are durable at-least-once and must be idempotent.

Do not claim exactly-once callback execution.

---

## Medium-severity findings

### F21 — Query equality is backend-dependent

**Locations:** `pipeline.py:122-143`, `sql.py:172-200`

Postgres uses JSONB containment, which is not strict equality for object/array
values. SQLite uses `json_extract == value`; filtering a missing path by `None`
matches SQL NULL and therefore also matches missing keys. The probe asking for
`meta.flag=None` returned both a record with explicit null and a record with no
`flag`. Dotted keys containing dots/special path syntax cannot be addressed
portably.

**Direction:** specify a predicate AST and exact cross-backend semantics, including
missing versus JSON null. Add identical SQLite/Postgres conformance tests.

### F22 — Subscription order contradicts its contract

**Locations:** `subscribe.py:66-72`, `dispatch/inline.py:34-38`

The docstring promises base-to-derived MRO order, but iterating `cls.__mro__`
produces derived-to-base. The probe observed `derived, base`.

**Direction:** reverse the MRO walk or change and consistently document the
contract. Add ordering tests spanning base and derived registrations.

### F23 — Extension Protocols promise more than the runtime can support

**Locations:** `seams.py:38-107`, `config.py:62-86`, `store/__init__.py:41-66`

`RowStore` is described as governing “where” rows live, but every operation is
bound to a SQLAlchemy Session and the Store always owns a SQL engine/transaction.
A non-SQL store cannot make append/head/outbox atomic through this UoW. Custom
codecs are accepted, but administrative reconstruction hardcodes built-in formats.

The runtime Protocol check verifies attribute presence, not signatures or
semantics; `json_documents=False` still satisfies the marker because only presence
matters. Codecs/interceptors are largely typed `Any` and not validated eagerly.

**Direction:** either narrow the seams honestly to SQL repository strategies and
closed encodings, or move transaction ownership behind a real Store backend that
atomically implements the entire commit operation. Prefer one meaningful boundary
over several nominal Protocols.

### F24 — Database constraints do not encode the stated invariants

**Locations:** `schema.py:53-119`

There are no checks for nonnegative/contiguous versions, valid kinds, nonempty
streams/queues/handler IDs, or consistency between a head and its log version.
Append-only behavior exists only because one method happens to call `INSERT`; the
ORM/session surface can update/delete log rows.

**Direction:** add the constraints the database can express, restrict mutation
APIs, and use an atomic compare-and-set head update tied to expected version. For
Postgres production guidance, document role/permission or trigger-based log
immutability if I1 must survive direct database access.

### F25 — Outbox draining holds long transactions and has weak indexing/metrics

**Locations:** `dispatch/outbox.py:29-81`, `schema.py:96-119`

Postgres row locks remain open while user handlers execute. A slow external call
therefore holds a database transaction for the whole batch. SQLite performs an
unlocked select, so concurrent drainers may deliver the same row. Queue-filtered
drains have only an `available_at` index, and the returned integer conflates
claimed, successful, and failed rows.

**Direction:** use short claim/lease transactions followed by delivery and a short
ack transaction; add `(queue, available_at, seq)`-appropriate indexes; expose
structured metrics and statuses; test concurrent drainers on both databases.

### F26 — Registry design impedes reloads, workers, and operations

**Locations:** `config.py:23-59`, `subscribe.py:22-63`

Class/function registries hold strong process-global references, derive IDs from
`module:qualname`, and rely on import side effects. Hot reload creates new class or
function objects and collides with the old entries. Refactoring a function strands
pending outbox rows. CLI/workers cannot discover application declarations without
an unstated import convention.

**Direction:** introduce an explicit immutable application catalog built at
startup, stable user-chosen stream/subscription IDs, collision validation, and
handler aliases for migrations.

### F27 — Hydration hook semantics are contradictory and potentially unsafe

**Locations:** `interceptors.py:1-7,27-29`, `pipeline.py:77-94`

Documentation says `after_hydrate` failures are logged and isolated; the code
propagates them. For examples such as decrypt/redact, silently continuing after a
failure could leak unsafe data, so the documentation's desired behavior is itself
questionable. Returned values are not revalidated and can violate the declared
Record type.

**Direction:** make hydration transformations fail closed, type them precisely,
and distinguish required transforms from observational callbacks. Do not group
security transforms under best-effort error isolation.

### F28 — Public typing is not at library quality

**Locations:** package-wide; especially `seams.py`, `event.py`, `config.py`,
`pipeline.py`, `subscribe.py`

The wheel has no `py.typed` marker. Core APIs use unparameterized `dict`,
`Callable`, `Any`, and untyped hook inputs/outputs. An ad hoc BasedPyright run using
the project interpreter reported 69 errors and 891 warnings across 25 files,
including an import cycle and missing generic arguments. There is no checked type
configuration defining a realistic supported baseline.

**Direction:** make Record/Event/Codec/Subscription generic where useful, type the
JSON value tree and callable signatures, eliminate the root/Record import cycle,
ship `py.typed`, and gate a deliberately configured checker.

### F29 — Performance APIs are unbounded

**Locations:** `sql.py:146-164,172-200`, `pipeline.py:112-119,149-197`,
`dispatch/outbox.py:55-62`

`history`, unfiltered/large `where`, `all_rows`, and head rebuild materialize full
result sets. Rebuild loads a stream, groups it in Python, and retains all rows and
groups. There is no pagination/streaming API or stable result ordering for
`where`. Handler execution is serial.

**Direction:** add cursor pagination and iterators, rebuild per aggregate or in
bounded chunks into a replacement projection, and publish complexity/memory
contracts. Benchmark realistic Postgres workloads rather than only counting SQL
statements on SQLite.

### F30 — Dependency and release claims are inconsistent

**Locations:** `pyproject.toml:12-34`, `README.md:7-9,170-176`,
`examples/webhook.py:18-24`

The concept says two dependencies, but Alembic and `python-dotenv` are mandatory.
`python-dotenv` is used only by an optional example and should not burden core
installs. The example calls `load_dotenv()` at import despite claiming import has
no side effects. Version is duplicated in `pyproject.toml` and `__init__.py`.

**Direction:** make runtime dependencies match the thesis, move example/integration
requirements to extras, derive one version source, and test minimal installation.

### F31 — Automated quality gates do not match the project's ambition

**Locations:** repository root, `pyproject.toml`, test suite

The README advertises `.github/workflows/ci.yml`, but no `.github` workflow is
tracked. Ruff reports five production unused-import errors, and 13 production/
migration files fail format checking. No lint, format, type, build-content,
migration-parity, or coverage gates are configured.

The core suite is broad for previously known bugs but overfits the prior review:
many test names and source comments encode F1-F23 history while missing ordinary
Pydantic/container and production-schema behavior. The DBOS webhook test also
failed to complete within 60 seconds in isolation, causing the full suite to stall.

**Direction:** establish a real CI matrix and favor behavioral/property tests over
historical implementation checkpoints.

### F32 — Historical review scaffolding has leaked into production code

**Locations:** most module docstrings and tests; sdist contents

Production docstrings repeatedly cite “Step N” and old finding IDs. This makes the
code read like an implementation diary and couples maintenance to hidden scratch
documents. The sdist then ships the entire diary, including old probes.

**Direction:** keep rationale in ADRs/design docs; make source comments explain
current invariants and non-obvious mechanics only. Exclude scratch projects from
release artifacts.

---

## Recommended target architecture

The repair should converge on a single invariant-preserving commit operation:

```text
Record command
  (stream, id, expected_version, proposed user input)
        │
        ▼
Store-scoped UnitOfWork
        │
        ├─ load durable head/canonical prior state
        ├─ compare expected_version atomically
        ├─ validate proposed domain state
        ├─ run required domain hooks and revalidate
        ├─ serialize ONE canonical JSON state
        ├─ encode(versioned codec/schema)
        ├─ append log row
        ├─ derive head BY APPLYING THAT ROW
        ├─ stage outbox by stable subscription_id
        └─ commit
             │
             ├─ materialize committed Record with committed_at
             ├─ return that exact committed value
             └─ best-effort inline dispatch of the same Event envelope
```

### 1. Separate domain state from commit metadata

`id`, `version`, `version_id`, and `committed_at` should not be ordinary
client-settable Pydantic inputs. Use private/internal envelope state with read-only
properties or a `Committed[T]` envelope around a user model. Public construction
must not be able to fabricate durable versions.

### 2. Make the aggregate key explicit

Prefer `(stream, id)` as aggregate identity. Include it in uniqueness, replay
comparison, version identity, head keys, and errors. Write a migration strategy
before changing deployed IDs.

### 3. Give one component ownership of atomic commit

The current RowStore methods expose too many intermediate states. A backend should
own an atomic `commit(request) -> CommitResult` contract (or a repository operating
inside a store-owned transaction). The pipeline should not be able to pair one
backend's session with another backend's methods.

### 4. Make log-to-head projection deterministic

The committed row must be sufficient to calculate the next head. The live projector
and rebuild projector must call the same function. A test should delete all heads,
rebuild them, and compare every byte to the pre-delete projection.

### 5. Version persisted encodings

Add at least:

- `schema_version`
- `encoding` / `codec_version`
- stable `stream`
- canonical event kind

Provide upcasters and retained decoders. Do not infer an encoding solely from a
boolean snapshot flag.

### 6. Replace import-side-effect discovery with an application catalog

Build an explicit catalog of streams, codecs, interceptors, and subscriptions at
application startup. The CLI and workers should accept a bootstrap target that
returns this catalog. Stable subscription IDs must be user-controlled and
migratable.

### 7. Treat delivery as a durable state machine

Use claim leases, delivery status, attempt/error metadata, dead-lettering, redrive,
and structured results. Never serialize credentials. State the at-least-once
contract precisely.

### 8. Choose honest extension boundaries

If only SQLAlchemy stores and Snapshot/Delta encodings are supported for 0.3, close
those sets and make them excellent. If arbitrary stores/codecs are a product goal,
move transaction and projection ownership behind those interfaces and prove each
extension with contract tests. Nominal pluggability that administration cannot
understand is worse than a small closed design.

## Implementation sequence

1. **Freeze release and add failing regressions.** Promote every blocker probe to
   the suite before changing code.
2. **Repair the commit model.** Make metadata internal, enforce expected-version
   CAS, canonicalize one state, return the committed object, and project from the
   encoded row.
3. **Repair Store/UoW scoping.** Bind UoWs to Stores, support read-your-writes,
   correct activation tokens, and test threads plus asyncio task contexts.
4. **Repair schema and migrations.** Fix SQLite PK/index drift, add encoding/schema
   fields and database checks, then verify upgrade and downgrade semantics on both
   supported databases.
5. **Repair delivery.** Canonical deltas/events, stable subscription IDs,
   bootstrap discovery, claim/ack leases, dead letters, truthful CLI results, and
   credential-free DBOS payloads.
6. **Define evolution.** Upcasters, codec transitions, handler aliases, and rolling
   deployment tests.
7. **Harden release engineering.** Package migrations, exclude scratch artifacts,
   add `py.typed`, CI, lint/format/type gates, wheel-install smoke tests, and schema
   parity checks.
8. **Only then optimize/document.** Pagination, chunked rebuilds, indexes,
   benchmarks, concise current-state docs, and precise delivery terminology.

## Required validation matrix

### State and Pydantic

- nested mutable list/dict/model mutations
- computed fields and aliases
- field/model serializers and validators
- UUID, datetime, date, decimal, enum, bytes, secrets, nested unions
- interceptor outputs that change type/id/version/class
- property-based sequences comparing head, exact reads, history, and full rebuild

### Concurrency and transactions

- same-base writers: one winner, loud losers
- update of unsaved/detached/fabricated versions
- same UUID across streams
- same Store nested/reentrant, different Stores nested
- one shared Store across threads and asyncio tasks
- read-your-writes and rollback
- commit failure after log flush, head projection, and outbox staging

### Schema and migration

- all behavior against `create_all` schema and Alembic schema
- `alembic check` clean on SQLite and Postgres
- 0.2 upgrade read through 0.3
- downgrade read through the actual prior release, or explicit refusal
- codec/schema evolution fixtures
- head rebuild exact equality and orphan removal

### Delivery

- inline/durable Event equivalence
- JSON-normalized deltas after Pydantic coercion
- overlapping subscriptions
- missing/renamed handlers and streams
- concurrent drainers, crash after claim, crash after side effect, ack failure
- retry exhaustion, dead-letter, and redrive
- CLI in a fresh installed-wheel process with application bootstrap
- no secrets in durable payloads

### Release

- lint, formatting, typing, warnings-as-errors, coverage
- minimal dependency install
- wheel contents and clean-wheel smoke test
- migration resources available from the wheel
- README commands executed from the installed artifact
- SQLite plus live Postgres CI

## Decisions that must be made explicitly

1. Is aggregate identity global by UUID, or scoped by stream?
2. Does “immutable Record” mean recursively immutable, or only assignment-frozen?
3. Is a custom codec a stable persisted-format plugin or only a runtime strategy?
4. Are non-SQL stores truly supported?
5. Are callbacks sync-only, async-only, or dual API?
6. Is `UnitOfWork` a public application transaction, with read-your-writes, or an
   internal write-batching mechanism?
7. Which delivery guarantee is promised for each transport?
8. What is the supported model/codec/handler evolution contract?
9. Is outbox reconstruction intended? If so, how is delivered state known? If not,
   stop calling it rebuildable.

## Bottom line

Do not polish around the current commit pipeline. Preserve the product thesis and
the small API, but rebuild the center around canonical persisted state and a
store-scoped compare-and-set transaction. Until the blocker probes are impossible
by construction—and the installed wheel can execute the documented migration and
delivery paths—0.3 should be treated as an architectural prototype, not a durable
event-history library.
