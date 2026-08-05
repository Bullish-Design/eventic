# 003 — Structural Refactor (eventic 0.3)

Third project in `.scratch/projects/`. Follows `001-code-review` (the 0.1 audit) and
`002-reimplementation` (the 0.2 rewrite). This one reviews **0.2 as shipped** and
specifies the refactor to **0.3**.

## Read in this order

| # | Document | What it is |
|---|---|---|
| 1 | [`REVIEW.md`](REVIEW.md) | 23 verified findings against 0.2 (`77084af`). Every one reproduced, not inferred. The evidence base. |
| 2 | [`CONCEPT.md`](CONCEPT.md) | **The revised concept (v3).** Supersedes `002-reimplementation/CONCEPT.md`. §11 maps each change to the defect that forced it. |
| 3 | [`IMPLEMENTATION_GUIDE.md`](IMPLEMENTATION_GUIDE.md) | **The route.** 7 phases, 24 steps, each with an exit gate and a rollback. |
| — | [`probes/`](probes/) | Runnable reproductions. `.venv/bin/python probes/<file>` |

## The short version

**Baseline:** `86 passed, 1 skipped` — the suite misses all 23 findings.

Twenty-three findings, three root causes:

1. **Plugins selected by inheritance** — framework classes land in the user's pydantic
   MRO, so five phantom fields get persisted into an append-only log forever, and
   subclassing a plugin-bearing Record either installs a `Record` as the codec or
   crashes at class definition.
2. **Six module-level mutable globals**, each with a `_reset_*` hook — the cause of a
   public API that did nothing (`use()`) and a "per-class" plugin that was secretly
   process-wide.
3. **Nothing owns the transaction** — I7 is violated on the DBOS path (handlers fire
   for versions that get rolled back), and neither the outbox nor a head projection
   has a natural home.

Six structural moves, in `CONCEPT.md`:

| Move | Kills |
|---|---|
| Seams selected by **class keyword**, not inheritance | F1, F2 |
| **Three** protocol seams, not five capability-token seams | F9, F12 |
| **The transaction emits**, not the pipeline | F3 |
| Delivery is a property of the **subscription** | F10 |
| Log + derived **head** + **outbox** triad | F16, F17 |
| Records are **frozen values**; `draft().commit()` returns the new version | F6, F14 |

Net effect: the library gets **smaller** while getting more correct. Roughly a third
of 0.2's public surface was speculative and is deleted outright — the capability-token
DSL, `use()`, `TypedTable`, `contribute_schema`, `full_state_rows`, the identity seam,
the delivery registry, `hair_trigger`, and all six `_reset_*` hooks.

## What must not regress

The append-only kernel, deterministic `uuid5` identity, and the optimistic lock.
`probe_06` races 8 threads at one `(id, version)` and gets **1 winner, 7 loud
`StaleVersionError`s**. I5 is the one thing 0.2 got unambiguously right; it is the
canary at every exit gate.

## Definition of done

`IMPLEMENTATION_GUIDE.md` Step 0 converts all 23 findings into `xfail(strict=True)`
tests. The refactor is complete when `pytest src/tests/regression -q` reports
**0 xfailed, 24 passed** — not when it feels finished.
