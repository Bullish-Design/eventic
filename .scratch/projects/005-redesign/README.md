# 005 — Ground-up Redesign (eventic 1.0)

Fifth project in `.scratch/projects/`. Follows `001-code-review` (the 0.1 audit),
`002-reimplementation` (the 0.2 rewrite), `003-structural-refactor` (0.2 → 0.3), and
`004-structural-refactor-review` (the 0.3 adversarial review).

This one is not a refactor. **There is no live data**, so 1.0 is a fresh start with no
compatibility surface, no shims, and no migration path from 0.x.

## Read in this order

| # | Document | What it is |
|---|---|---|
| 1 | [`CONCEPT.md`](CONCEPT.md) | The thesis, the one root cause, the vocabulary, ten invariants, and the sealed-versus-open line. |
| 2 | [`ARCHITECTURE.md`](ARCHITECTURE.md) | Module graph, public types, the seven-method store contract, physical schema, delivery state machine, and the ten async-readiness rules. |
| 3 | [`IMPLEMENTATION_GUIDE.md`](IMPLEMENTATION_GUIDE.md) | Seventeen phases, each with steps, required tests, and an exit gate that is a command. |
| — | `../003-.../REVIEW.md`, `../004-.../REVIEW.md` | The evidence base. 55 verified findings, all reproduced against shipped code. |

## The short version

Three reviews, ~70 findings, one cause:

> **`Record.save()` is ActiveRecord, and ActiveRecord requires ambient global state.**

Every global, every `ContextVar`, every `_reset_*` hook, every registry, and every
"nothing owns the transaction" defect is downstream of wanting `todo.save()` to work
without naming a database. Delete the affordance and the machinery built to support it
becomes unnecessary rather than better.

Six moves:

| Move | Deletes |
|---|---|
| State is the user's plain `BaseModel`; eventic owns `Revision[T, M]` | `Record`, `Draft`, managed-field input, computed-field poisoning, the mixin-vs-keyword debate |
| Operations live on a store-bound `Collection`, never on the value | every global, every `ContextVar`, cross-store writes |
| One canonical document + `sha256` digest drives log, head, event, and return value | the entire "two parts disagree" blocker class |
| The store owns atomic commit — one method, one round trip | wrong-database writes, non-atomic staging |
| Compare-and-swap on `expected_revision`, taken inside the transaction | revision gaps, stale-handle corruption, read-your-writes ambiguity |
| Two extension points: `Store` and subscription handlers | interceptors, seams, capability tokens, plugin bases, the application compiler |

Net effect: 1.0 is **smaller than 0.3**, not larger. 004's `PLUGIN_FRAMEWORK.md`
proposes fifteen runtime protocols and a twenty-step application compiler for a library
with zero third-party extensions; `CONCEPT.md` §7.3 maps each proposal to where it
actually lands. 004's real contributions — canonical-state discipline, stable
subscription ids, the sealed kernel, typed metadata, a delivery state machine, sync/async
explicitness — are adopted in full.

## Async

1.0 ships synchronous. `ARCHITECTURE.md` §9 defines ten rules that keep the future port
to ~350 lines *below* the store protocol, with zero edits above it. Every one of those
rules is independently forced by a 003/004 finding, so async-readiness costs nothing but
naming discipline. Phase 16 proves it with a paper port before the decision is ever due.

## What must not regress

The append-only kernel, deterministic `uuid5` identity, and the optimistic lock. 003's
`probe_06` raced 8 threads at one `(id, version)` and got **1 winner, 7 loud errors**.
That is the one thing every version got right; it is a scenario in the store conformance
suite and a gate on both backends.

## Definition of done

`CONCEPT.md` §12 — fifteen statements, each a test, not a judgement. The first real
checkpoint is `IMPLEMENTATION_GUIDE.md` Phase 8: an end-to-end vertical slice on SQLite
with the four-way agreement property green. **If anything in Phases 1–8 required a
workaround, stop and reassess the design there** — everything after is plumbing on a
core that is either right or wrong.
