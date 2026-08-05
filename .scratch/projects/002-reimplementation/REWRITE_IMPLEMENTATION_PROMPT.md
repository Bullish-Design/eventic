# Eventic — Full Rewrite Implementation (Kickoff Prompt)

> Paste this whole document into a fresh agent session to begin the rewrite. It is
> self-contained: it tells you what to read, how to work, what to build, the
> non-negotiable invariants, and how to know each step is done. Do not start coding
> until you have read §1's documents and reproduced the green baseline.

---

## 0. Your role

You are a **senior Python engineer executing a fully-designed rewrite**. The
architecture, the invariants, and the step-by-step plan already exist and were
produced by an adversarial first-principles review (see §1). Your job is **not** to
re-litigate the design — it is to **implement `REWRITE_GUIDE.md` faithfully, one
commit per step, keeping the test suite green (or red-by-design in TDD) at every
gate**, and to record any necessary deviation honestly rather than silently drifting.

You may — and should — push back if you discover the guide is *wrong* (a code sketch
that doesn't compile, an ordering that breaks a gate, an invariant that can't hold as
written). When that happens, verify with a probe, fix the approach, and record the
deviation in an execution-notes appendix (see §5). Do not gold-plate, do not add
seams or plugins the guide doesn't call for, and do not "improve" the scope.

Work in `eventic` (`https://github.com/Bullish-Design/eventic`). All planning
artifacts live in `.scratch/projects/002-reimplementation/`.

---

## 1. Read these first, in this order (do not skip)

1. `.scratch/projects/002-reimplementation/CONCEPT.md` — the irreducible idea and the
   **seven invariants (I1–I7)** and the write/read pipeline. These invariants are the
   spec; everything you write must uphold them.
2. `.scratch/projects/002-reimplementation/PLUGINS.md` — the closed **five-seam**
   plugin framework (persistence, codec, identity, delivery, interceptor) and its
   anti-gold-plating guardrails (§8). Internalize §8.5: *do not build the framework
   before the second real plugin needs it.*
3. `.scratch/projects/002-reimplementation/REWRITE_GUIDE.md` — **your build script.**
   14 steps in 6 phases, each with code sketches, a test list, an exit gate, and a
   rollback. You will follow this document step by step.
4. `.scratch/projects/002-reimplementation/TARGET_ARCHITECTURE.md` — the target public
   API surface and the three canonical code examples (hello-world / medium / power).
5. `.scratch/projects/002-reimplementation/REIMAGINE_REVIEW.md` — the *why*: the
   findings (`R-C1`…`R-X2`) the rewrite exists to close. Skim §2–3; the guide's
   finding→step map tells you which step closes each.
6. `.scratch/projects/002-reimplementation/probes/` — four runnable probes that prove
   the current bugs (silent lost-update, hidden writes, DBOS cost, pickle RCE). Re-run
   them; they become regression targets (the rewrite must make probe_02's scenario
   *raise*, not lose data).
7. The current source under `src/eventic/`, `src/tests/`, `pyproject.toml`,
   `migrations/`, `dbos-config.yaml` — the code you are replacing and must migrate
   from without data loss.

Then run the baseline: `.venv/bin/python -m pytest src/tests -q` → **must be 28
passed** before you change anything. Deps in the repo `.venv`: dbos 2.29.0,
sqlalchemy 2.0.51, pydantic 2.11.7, fastapi 0.115.13, alembic 1.19.0.

---

## 2. What you are building (one paragraph)

A rebuilt eventic whose **core depends only on pydantic + SQLAlchemy** and whose
identity is "*a Pydantic model becomes a versioned aggregate whose version log is an
event stream.*" Writes are **explicit** (`save`/`update`/`edit`/`commit`) — pure
construction does no I/O. Concurrency conflicts are **loud** (`StaleVersionError`),
not silent. Events fire **post-commit**, synchronously by default. Everything beyond
the invariant core — durable async (DBOS), diff storage, typed columns — is a
**plugin** attached at a named seam and validated at class-definition time. DBOS is
an **optional extra** (`eventic[dbos]`), never in the core import graph.

The full target surface, module map, and DDL are in `TARGET_ARCHITECTURE.md` and
`REWRITE_GUIDE.md`. Do not invent beyond them.

---

## 3. The invariants you must never break (from CONCEPT.md §3)

Every commit is checked against these. A change that violates one is wrong even if the
tests pass.

- **I1 Append-only** — committed versions are immutable; writes only add rows.
- **I2 No hidden writes** — only `save`/`update`/`commit`/`edit` persist; nothing else.
- **I3 Pure construction** — `Model(...)` is in-memory only, no I/O.
- **I4 Deterministic identity** — `version_id = uuid5(NS, "eventic:{id}:{version}")` for
  **all** versions incl. v0.
- **I5 Loud conflicts** — two different writers at one `(id, version)` raise
  `StaleVersionError`; only a byte-identical replay is a silent no-op.
- **I6 Core is DBOS-free** — `import eventic` must not import `dbos`/`fastapi`.
- **I7 One post-commit event** — events fire after durability, exactly once per commit.

Add a test that *directly asserts* each invariant (the guide names them:
`test_construction_writes_nothing`, `test_two_writers_raise_stale_version`,
`test_handler_sees_committed_row`, the DBOS-free import assertion, etc.). These are the
contract; keep them green from the step that introduces them onward.

---

## 4. Method & working conventions

- **Branch:** `git checkout -b rewrite/plugin-core` off `reimagine/first-principles`.
  One commit per guide step, message `Step N: <what>` (mirror the 001 guide's style).
- **TDD where the guide says so:** write the step's named tests first; some are meant
  to be red until the step's code lands. A step is done only when its exit-gate tests
  are green and the previously-green suite has not regressed.
- **Keep old and new side by side until Phase 6.** Do not delete any current module
  before Step 12. New code goes in the new modules/`src/tests/{core,plugins,dbos}`; the
  old suite keeps proving old behavior until the swap.
- **Verify claims, don't trust sketches.** The guide's code is a sketch, not gospel.
  Before committing the two risky steps — **Step 7** (DBOS transactional-outbox
  atomicity) and **Step 8** (diff reconstruction across a snapshot boundary) — write a
  throwaway probe under `probes/` proving the mechanism actually works on the repo's
  `.venv` (SQLite). Check DBOS behavior against the installed source in
  `.venv/lib/python3.13/site-packages/dbos/`, not memory.
- **SQLite-first.** Everything must be testable on SQLite with no Postgres. Mark
  Postgres-only assertions (JSONB `@>`, GIN, serialization retry) with a `postgres`
  marker as today.
- **Record deviations.** When you must depart from the guide's literal text, append an
  entry to `REWRITE_GUIDE.md`'s new **"Appendix — Execution notes & deviations"**
  (dated, one row per deviation, with the reason), exactly as the 001 guide's Appendix
  B did. This is how the next reader learns where the plan met reality.
- **Working log.** Keep appending dated entries to
  `.scratch/projects/002-reimplementation/LOG.md`: what you built, what you verified,
  what you decided, per session.
- **Each phase ends shippable.** Tag `0.2.0-alpha` after Phase 1, and keep the
  post-Phase-3 and post-Phase-4 states shippable. If you run out of time, stop at a
  phase boundary, never mid-phase.

---

## 5. Constraints & boundaries (non-negotiable)

1. **The demo and public claims must keep working** at the end: `python -m
   eventic.examples.demo`, the FastAPI webhook, hydration, versioned history, `meta`,
   events, and (opt-in) durable queues must be demonstrable on the new API — or you
   must explicitly argue in the log/README why one was dropped.
2. **No data loss.** The `properties → data.meta` migration (Step 10) must be
   reversible and validated on a production-shaped copy; keep the shipped C6 backfill
   in the Alembic chain. A table written by the OLD library must hydrate under the NEW
   one.
3. **DBOS is optional.** After Phase 6, `pip install eventic` (no extras) must import
   and run the core suite with `dbos` absent from `sys.modules`.
4. **No gold-plating.** Implement the five default plugins + exactly the two real
   plugins the guide specifies (DBOS delivery, diff codec). Do **not** build
   `TypedTable`/SQLModel, multi-tenancy, encryption, etc. — they are documented
   *demonstrations of reach* in PLUGINS.md, not scope. Leave `TypedTable` as a
   documented stub interface only.
5. **Fail fast, at class definition.** Plugin conflicts and unmet requirements must
   raise when the `Record` subclass is defined — never at import time, never at first
   call. This is a hard requirement (it's the fix for the same-name-class crash).

---

## 6. Deliverables

1. **The rewritten library** under `src/eventic/` matching the `REWRITE_GUIDE.md`
   module map: core (`record`, `pipeline`, `connect`, `models`, `events`, `errors`),
   `plugins/` (the five seams with defaults + `DiffStorage`), `dbos/` (the optional
   delivery plugin + `create_app`/`durable`/`queue`), rewritten `examples/`.
2. **A behavioral test suite** under `src/tests/{core,plugins,dbos}` — one test per
   invariant, plus the per-step tests the guide names; the DBOS suite gated on the
   `[dbos]` extra; the old suite deleted in Step 12.
3. **Migrations** — a fresh initial revision for the new schema, the
   `fold_properties_into_data` revision (reversible), the retained C6 backfill; all
   round-tripping on SQLite and Postgres.
4. **Docs** — README rewritten to the CONCEPT §9 positioning; `MIGRATION.md` for
   0.1.x → 0.2; `pyproject.toml` with core deps trimmed and the `[dbos]` extra.
5. **Probes** for the two risky mechanisms (Steps 7, 8), numbered, under `probes/`.
6. **Updated `LOG.md`** and the new deviations appendix in `REWRITE_GUIDE.md`.

---

## 7. Success criteria (the final validation matrix — Step 13)

The rewrite is done when every row passes:

| check | command |
|---|---|
| Core import is DBOS-free (I6) | `python -c "import sys, eventic; assert 'dbos' not in sys.modules"` |
| Core suite is fast (R-P3) | `pytest src/tests/core -q` completes in ~1s, all green |
| Plugin suite | `pytest src/tests/plugins -q` green |
| DBOS suite | `pip install -e '.[dbos]' && pytest src/tests/dbos -q` green |
| No hidden writes (I2/I3) | `test_construction_writes_nothing` green |
| Loud conflicts (I5) | `test_two_writers_raise_stale_version` green (probe_02 scenario now raises) |
| Post-commit events (I7) | `test_handler_sees_committed_row` green |
| Plugin conflict fails at definition | `test_two_codecs_conflict` green |
| Migrations round-trip | `alembic upgrade head && alembic downgrade base` on SQLite + PG |
| Warnings clean | `pytest -W error` green |
| Demo works | `python -m eventic.examples.demo` runs end-to-end |
| One commit per step | `git log` shows Step 0…Step 13 |

And the judgment call: **a newcomer can read the new `src/eventic/` in one sitting and
predict what `Model(...)`, `.save()`, and `.update()` do** — because there is no
metaclass, no hidden write, and no singleton to hold in their head.

---

## 8. How to begin

1. Read §1's seven documents; re-run the four probes; reproduce the 28-green baseline.
2. `git checkout -b rewrite/plugin-core`.
3. Start at `REWRITE_GUIDE.md` **Step 0** and proceed in order. Do not jump ahead; do
   not skip a step's exit gate. Commit at each gate.
4. At Steps 7 and 8, write the verifying probe *before* the implementation.
5. Log as you go; record deviations in the guide's appendix.

*Begin by reading the four core documents (CONCEPT, PLUGINS, REWRITE_GUIDE,
TARGET_ARCHITECTURE), running the probes, and confirming the baseline — before writing
any library code.*
