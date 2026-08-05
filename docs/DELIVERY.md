# Delivery semantics

## Staging

At commit time, every subscription whose stream matches the commit, whose
`kinds` contains the commit kind, and whose `delivery` is `Outbox` produces one
delivery intent, written in the same transaction as the log and head rows
(I8). Inline subscriptions produce no rows.

## The state machine

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

The worker loop, per batch:

1. **Claim** — one short transaction: select `status='pending' AND
   available_at <= now()` (plus expired leases) for the queue, mark `leased`,
   set `leased_until`, bump `attempts`.
2. **Deliver** — outside any transaction. Load the revision, upcast, hydrate,
   build `Commit`, call the handler. No database lock is held while user code
   runs.
3. **Settle** — one short transaction: delete on success; on failure compute
   the disposition purely (retry with exponential backoff, or dead-letter) and
   apply it.

Expired leases return to `pending` implicitly: the claim query treats
`status='leased' AND leased_until < now()` as claimable.

## The contract

Delivery is **at-least-once**. A side effect may succeed and the ack may fail;
the intent is then delivered again. Handlers must be idempotent.

`WorkerReport` counts `claimed`, `delivered`, `retried`, and `dead_lettered`;
`eventic worker --once` exits non-zero when any intent was dead-lettered.

## Error handling

- Inline failures are collected — every handler still runs — and raised as
  `InlineDispatchError` (or logged with `App(on_inline_error="log")`). The
  commit is already durable and is not affected.
- `last_error` on an intent is redacted (credentials stripped, truncated to
  2 KiB) so a handler failure never leaks a secret or a payload.
