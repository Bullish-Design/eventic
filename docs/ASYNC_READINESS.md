# Async-readiness audit

1.0 ships synchronous. The async port is expected later; these rules keep it a
set of new files *below* the store protocol, with zero edits above it. Every
rule is enforced by `tests/architecture/test_async_ready.py` (R1–R10).

## The rules (each forced by a review finding)

1. `Store` is seven methods, one request in, one value out — no callbacks, no
   session arguments, no returned transactions.
2. No generators or lazy iterators cross the I/O boundary; reads return
   `Page`.
3. Zero I/O above `protocols.py` — `planning`, `hydration`, `canonical`,
   `evolution`, `retry` are pure functions over values.
4. SQL is data: `sql/statements.py` builds constructs and executes nothing.
5. No SQLAlchemy type appears in a protocol or public signature.
6. Handler color is decided at declaration; `App` rejects coroutine handlers.
7. Two concrete protocols later — never one `Awaitable[T] | T` generic.
8. Conformance suites are declarative scenarios plus a thin runner; the async
   suite is a second runner, not a copy.
9. No `ContextVar`, no thread-local, no module-level mutable state.
10. `StoreAdmin` is sync forever.

## The measured port (paper, 2026-08-05)

A paper port — `AsyncStore` protocol signatures and an `AsyncSqlStore`
skeleton with `NotImplementedError` bodies — imported cleanly against the
shipped package:

| Component | Port size |
|---|---|
| `protocols_async.py` (signatures) | ~55 lines |
| `async_store.py` (`.execute()` → `await .execute()`) | ~57 lines |
| `statements.py`, `planning.py`, `hydration.py`, `retry.py`, `canonical.py`, `evolution.py` | **no edits** |

The skeleton lives in `.scratch/projects/005-redesign/async-port/` as
evidence; it is not shipped. The full port adds an async `runtime` (~120
lines), an async `worker` (~80 lines), and an optional `AsyncRuntime`
bridging via `asyncio.to_thread` (~80 lines) — roughly 350 lines below the
protocol line and nothing above it.
