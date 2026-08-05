# Invariants

Each invariant names the mechanism that makes violating it impossible to
write. An invariant enforced by a test is a convention; these are enforced by
construction.

| # | Invariant | Made true by |
|---|---|---|
| **I1** | Append-only. A committed revision is never modified or deleted. | The store exposes no update or delete path for log rows. Production guidance: grant the application role `INSERT`-only on `eventic_revision`. |
| **I2** | The log is the only truth. Heads, intents, and any projection are derived and byte-exactly rebuildable from the log. | Every log row carries the digest of its logical document; `eventic verify` recomputes heads from the log and compares digests. |
| **I3** | One canonical document. The log row, head row, returned `Revision`, and emitted `Commit` all derive from the same canonical bytes. | The commit path serializes once; the head is derived by decoding the row just encoded, and a digest mismatch aborts the transaction. |
| **I4** | Pure declaration. Constructing state, a `Stream`, a `Subscription`, or an `App` performs no I/O and touches no global. | No module-level mutable state exists in the package; declarations are frozen values validated in their constructors. |
| **I5** | Explicit, store-bound writes. Persistence happens only through a `Collection` obtained from a `Runtime` bound to a `Store`. | No ambient store, no `ContextVar`, no method on the state model. |
| **I6** | Deterministic identity. `revision_id = uuid5(NS, f"{stream}:{id}:{revision}")`; the aggregate key is `(stream, id)`. | One module function used everywhere; `(stream, aggregate_id, revision)` is the unique constraint. |
| **I7** | Loud conflicts. Every write carries an `expected_revision`, compared against the durable head inside the commit transaction. | Compare-and-swap in the store; replay is a silent no-op only when every durable field matches. |
| **I8** | Atomic commit. The log row, the head row, and every delivery intent are written in one transaction, or none of them are. | One store method, `commit(batch)`. Orchestration never sees a session. |
| **I9** | Post-durability dispatch, honestly named. Inline handlers run after `COMMIT`; outbox delivery is at-least-once and must be idempotent. | Inline dispatch is unreachable before `commit` returns. |
| **I10** | Decodable history. Every row records `schema_version` and `encoding` sufficient to decode it. | Both are non-null columns with check constraints; the upcaster chain is validated at `App` construction. |
