# Eventic — First-Principles Reimplementation Review (Prompt)

> Paste this whole document into your agent session to kick off the review. It is
> self-contained: it tells you what to read, how to think, what to attack, and what
> to deliver.

---

## 0. Your role

You are a **senior Python/systems architect conducting an adversarial review with
license to rebuild**. Your mandate is NOT incremental bug-fixing — the incremental
pass already happened (see Context). Your mandate is to **step back, re-derive what
this library is *for* from first principles, and design the best, cleanest, most
elegant codebase and architecture that could deliver that goal** — even if that
means throwing away large parts of the current implementation, as long as you can
justify every deletion or replacement with evidence and a migration story.

Be a fierce critic of the *current design*, but an equally fierce critic of your
own redesign: flag over-engineering, speculative generality, and "second-system"
gold-plating wherever you see them.

Work in `eventic` at `https://github.com/Bullish-Design/eventic` (current checkout
on `main`, `HEAD` = the completed 8-step refactor). Everything you produce lives in
`.scratch/projects/002-reimplementation/`.

---

## 1. Context: what this library currently is

Eventic's elevator pitch (from its own README): *"Pydantic, on a hair-trigger —
plain Pydantic v2 models become immutable, version-tracked aggregates that persist
to Postgres/SQLite and ride on DBOS durable queues & workflows."*

Current shape (after the `001-code-review` refactor, branch merged to `main`):

- **`src/eventic/`**
  - `core/record.py` — `Record` (Pydantic `BaseModel` + custom `RecordMeta`
    metaclass): copy-on-write `__setattr__`, durable v0 on construction,
    deterministic `version_id`, `hydrate`/`where`, `_commit` reflection.
  - `core/properties.py` — `PropertiesBase` free-form JSONB bag, auto-persists via
    an `_owner` back-pointer (`PrivateAttr`).
  - `persistence/models.py` — single `records` table (portable JSON/JSONB variant,
    `UNIQUE(id, version)`).
  - `persistence/store.py` — `RecordStore` with a DBOS-ambient-or-standalone
    `_session()`, `append`/`latest`/`stream`/`find_by_properties`.
  - `queues/dispatcher.py` — opt-in `@evented` decorator scheduling methods on a
    per-class DBOS `Queue` via `DBOS.step()`-registered wrappers.
  - `events.py` — global `EventRegistry` keyed by class object; `on.create` /
    `on.update` decorators; handler isolation.
  - `runtime.py` — `Eventic(DBOS)` facade: `init`/`create_app`/`reset`/`launch`/
    `queue`/`instance`; `_normalize_db_url`.
  - `bootstrap.py` — `init_eventic(engine)` store injection.
  - `main.py` — FastAPI webhook demo (`WebhookStory`, strict `WebhookPayload`).
  - `examples/demo.py` — showcase: `Story`, DBOS steps/workflow, queue usage.
- **`migrations/`** — Alembic initial revision + C6 backfill.
- **`tests/`** — 28 tests, all green on SQLite (`pytest src/tests -q`), green under
  `-W error`.
- Stack: Python ≥3.13, Pydantic 2.11, SQLAlchemy 2.0.51, **DBOS 2.29.0** (mandatory
  dep), FastAPI, Alembic, psycopg3.

### Read these first (in order)

1. `README.md` — the current public contract and claims.
2. `.scratch/projects/001-code-review/REVIEW.md` — the original adversarial review
   (findings C1–C6, H1–H8, M1–M12, L1–L12) — 6 critical, 8 high, 12 medium findings.
3. `.scratch/projects/001-code-review/REFACTORING_GUIDE.md` — the 8-step plan and,
   crucially, **Appendix B** (execution notes & deviations) — it records where the
   incremental approach itself was wrong or awkward.
4. Every file under `src/eventic/`, `src/tests/`, plus `pyproject.toml`,
   `dbos-config.yaml`, `migrations/env.py`.
5. Run the suite and the probe scripts to verify claims yourself
   (`.venv/bin/python -m pytest src/tests -q`).

The 001 review's findings are historical context — do not treat them as a spec.
Treat them as *data points about where the design fights itself*.

---

## 2. The first-principles re-derivation (do this before writing any findings)

Before critiquing implementation details, answer these questions in writing. Each
answer should be one short paragraph with an explicit "therefore our design must/need
not X" conclusion. Revisit and correct them after the adversarial pass — your final
report must show the delta between your initial answers and your conclusions.

### 2.1 The job-to-be-done
- Who is the user? (a solo dev building a small-to-mid-size event-driven web app? a
  team? a library author?) What concrete jobs do they hire eventic for?
- What is the **irreducible core** — the 20% of features that deliver 80% of the
  value? What is genuinely optional?

### 2.2 Core abstractions — are these the right ones?
Attack each fixed point of the current design and decide: keep / replace / delete.
- **Per-attribute copy-on-write** (`s.title = x` → new version row). Is implicit
  versioning on every write the right model, or should writes be explicit
  (`s.update(title=x)` / `s.commit()`)? Consider: write amplification, surprise
  writes from incidental attribute sets, the frozen-model hack, the metaclass.
- **`records` single-table schema.** One table for all aggregates/versions, JSONB
  `data` + `properties`. vs: per-aggregate tables, columns for known fields, no
  properties bag, or an append-only event log. Consider query ergonomics
  (`where` on JSONB), indexing, partial-select cost, and the `data`/`properties`
  duplication.
- **Durable v0 at construction.** Creating an object writes to the DB immediately.
  Is that a feature or a footgun? (Tests, background threads, "just build a model"
  use cases.)
- **The metaclass + per-class queue + `@evented`.** Is any of this needed? Is the
  "one process, one class name" limitation (queue registry keyed by derived name)
  acceptable? Would a plain function-level API (`Eventic.queue(...).enqueue(...)`)
  be more honest?
- **`PropertiesBase` with an `_owner` back-pointer.** The owner pointer is what
  makes `props.add()` persist — but it also creates reference cycles, coupling, and
  stale-bag footguns. Is the properties bag a first-class citizen or a crutch?
- **The event system.** `on.create` / `on.update` handlers keyed by class object,
  fired synchronously from inside model lifecycle. Is this the right event model?
  Does it conflict with DBOS's own workflow/notification machinery? Should events
  be async, outbox-based, or derived?
- **DBOS as the substrate.** What does DBOS genuinely buy (durable queues,
  workflows, retries, recovery) vs what does it cost (global singleton registry,
  same-name-class limitation, pickle-based default serializer for queue args,
  `sql_session` assertion dance, version pinning, the `_session()` fallback hack)?
  Would a thinner dependency (SQLAlchemy + a small queue lib, or Postgres
  LISTEN/NOTIFY, or plain threads for SQLite) be cleaner? Argue with evidence from
  the code, not vibes.
- **`Eventic(DBOS)` facade + singleton.** Is subclassing DBOS and holding a
  process-wide singleton the right shape? What happens in multi-app / test /
  long-running-service contexts?

### 2.3 What should the *final* public API look like?
Sketch the ideal public surface: package layout, exports from `eventic`,
`Record`-class API, store/query API, events API, async story. Show a "hello world"
(5 lines), a "medium app" (webhook + a couple of async jobs), and the "power user"
(versioned history query, migrations, multi-class queries) examples.

---

## 3. The adversarial checklist (attack the current code AND your redesign)

For each dimension, list concrete findings with severity (Critical / High / Medium /
Low) and mark claims **[verified]** with a probe like the 001 review did.

### 3.1 Correctness & data integrity
- Concurrency: two writers to one aggregate; queue workers racing hydrators;
  DBOS `SERIALIZABLE` retry vs `ON CONFLICT DO NOTHING` silently swallowing real
  conflicts in scripts.
- Crash recovery: replay idempotency of construction vs mutation (v0's version_id
  is still random — is that a replay hazard?).
- Event timing: update handlers run pre-commit; create handlers run post-append
  but outside any transaction in the standalone path. Is the documented
  "eventually-consistent within the emitting transaction" caveat acceptable, or a
  design smell that should be designed away?
- The `_session()` fallback: `try: DBOS.sql_session except AssertionError` — is
  there a cleaner boundary? What happens inside a DBOS **step** (not transaction)?

### 3.2 Security
- The default **pickle** serializer for queue args (`@evented` snapshots, enqueued
  records) — remote-code-execution surface if inputs ever cross trust boundaries.
  Should enqueue args be constrained (ids + JSON-serializable) or the serializer
  swapped?
- Webhook: is `WebhookPayload` strict enough? Extra fields, field size limits,
  content-type handling, error responses.
- Anything else attacker-influenced reaching the DB, logs, or queue.

### 3.3 API ergonomics & footguns
- Name a dozen things a new user will trip on (e.g., `Story()` writes to the DB;
  `s.version = 1` raises; `hydrate` raises `KeyError`; same-name classes crash the
  process; `props` references go stale; `Eventic.init` once-per-process).
- Are errors actionable? Do docstrings match behavior?
- Is the "free-form properties vs typed fields" split confusing? (why `status` in
  `properties` instead of a field?)

### 3.4 Performance
- Write amplification: every `props.add()` = full `model_dump` + full-row insert +
  reflection. Every mutation re-validates the whole model.
- `where()` on SQLite is O(all rows of a class) in Python; on Postgres it needs a
  JSONB containment index you don't provide. Is JSONB querying worth keeping?
- Read path: `hydrate` = full row + JSON decode; version history = N rows.

### 3.5 Testability, maintainability, architecture
- Module count, coupling direction, and hidden global state (registry, singleton,
  `_store` class vars, `_owner` pointers).
- Can a newcomer understand the whole library in one sitting? What's the
  "aha-heavy" machinery (metaclass, back-references, copy-on-write) that could be
  deleted outright?
- Is the test suite the right contract? Does it test behavior or implementation?
- `dbos-config.yaml`, migrations, and packaging: still coherent?

### 3.6 Ecosystem & redundancy
- What would you get by composing **existing, boring tools** (SQLAlchemy +
  pydantic, `SQLModel`, eventsourcing libs, `arq`/`dramatiq`/`huey`, Postgres
  `NOTIFY`, temporal-like engines)? What does eventic genuinely add that justifies
  existing at all? Answer this honestly — "delete the library" is a valid
  conclusion if the argument is strong.
- Which current features are kept "because the README says so" vs "because a real
  user needs it"?

---

## 4. Constraints & boundaries (non-negotiable)

1. **The demo and the public claims must keep working** — whatever the new
   architecture, `python -m eventic.examples.demo`, the FastAPI webhook, hydration,
   versioned history, properties, events, and queues must remain demonstrable,
   unless you explicitly argue one of them should be deleted and the README
   updated accordingly.
2. **Everything must be testable on SQLite without Postgres** (current suite is 28
   green incl. `-W error`); Postgres-only behavior must be cleanly marked.
3. **Python ≥3.13, keep or justify every dependency.** If you keep DBOS, justify it;
   if you drop it, show the replacement and the migration cost.
4. **No gold-plating**: prefer fewer abstractions; a smaller library that does the
   core beautifully beats a bigger one that does everything.
5. **The migration story matters**: 0.1.5 users exist (the demo, the webhook, the
   records table). Your plan must say how to get from today's repo to the target
   without data loss and with a boring, reviewable path.

---

## 5. Method & working conventions

- Read everything; do not trust docstrings or the README — verify claims against
  the installed dependencies' source (`.venv/lib/python3.13/site-packages/`) and
  with **live probes** in `.scratch/projects/002-reimplementation/probes/`.
- Run the test suite before you start and after any prototype you build.
- Every finding: severity, file/line, one-sentence impact, **[verified]** /
  **[inferred]** tag.
- Work on a branch off `main`, e.g. `reimagine/first-principles`.
- Keep a working log in `.scratch/projects/002-reimplementation/LOG.md` — dated
  entries, one per session/day, recording what you read, what you decided, and why.

---

## 6. Deliverables (in `.scratch/projects/002-reimplementation/`)

1. **`REIMAGINE_REVIEW.md`** — the adversarial review:
   - Section 1: first-principles re-derivation (2.1–2.3) with before/after answers.
   - Section 2: findings by dimension and severity (3.1–3.6), each tagged.
   - Section 3: **the verdict** — keep current shape with adjustments / thin
     rewrite / full rewrite — with the strongest possible argument for and against.
2. **`TARGET_ARCHITECTURE.md`** — the proposed final architecture:
   - Public API surface (with the three code examples from 2.3).
   - Module map + data model (schema DDL for the new storage).
   - What gets deleted, what gets renamed, what gets added — with reasons.
3. **`REIMPLEMENTATION_PLAN.md`** — a step-by-step plan (like the 001 guide: one
   commit per step, explicit exit gates, rollback notes) from today's `main` to the
   target architecture, including the data migration for existing `records` tables
   and the README rewrite.
4. **`probes/`** — every probe script you ran, numbered, one per claim cluster.
5. **`LOG.md`** — the working log.

## 7. Success criteria

The review is done when you can answer, with confidence and evidence:
- What is the minimal, most elegant form of "versioned Pydantic aggregates + durable
  async work"?
- What is the cheapest path from today's codebase to that form?
- What should be deleted, and what new name/README would the library deserve?
- The plan's first 3 steps can be executed without breaking the suite at each step.

---

*Begin by reading the four documents in §1, running the suite, and writing your
first-principles answers to §2 before touching any code.*
