# Operations

Every command takes `--app module:attr` (an `App` value) and `--url` (or
`$EVENTIC_URL`). Exit codes: `0` success, `1` operational failure, `2`
usage/config error, `3` drift detected.

| Command | Behavior |
|---|---|
| `eventic schema upgrade` | run migrations (Alembic) |
| `eventic schema check` | fingerprint and structural drift; exit 3 on drift |
| `eventic heads rebuild [--stream S] [--chunk N]` | truncate the scope in-transaction, rebuild heads from the log, compare digests |
| `eventic verify [--stream S] [--chunk N]` | stream the log in chunks, reconstruct every revision, compare against stored digests, compare rebuilt heads to live heads |
| `eventic worker --queue Q [--once]` | drain the queue; prints `WorkerReport`; exit 1 if any intent dead-lettered |
| `eventic intents list [--status dead]` | paged listing of delivery intents |
| `eventic intents redrive --subscription ID` | move dead intents of one subscription back to pending |
| `eventic inspect` | the resolved app: streams, schema versions, fingerprints, subscriptions with delivery and queue, store capabilities |

No command prints a connection URL or a payload. `inspect` prints every fact
that affects a commit; if a behavior is invisible there, it is a design bug.

## Production guidance

Grant the application role `INSERT` and `SELECT` on `eventic_revision` only.
The append-only invariant (I1) should survive direct database access: a role
that cannot `UPDATE` or `DELETE` the log cannot violate it.
