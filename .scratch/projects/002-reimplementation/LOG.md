# Reimplementation — Working Log

## 2026-08-04 — Session 1 (branch `reimagine/first-principles`)

**Read (in the §1 order):** `README.md`; `001-code-review/REVIEW.md` (C1–C6, H1–H8,
M1–M12, L1–L12); `001-code-review/REFACTORING_GUIDE.md` incl. **Appendix B**
(execution deviations — the honest record of where the incremental plan fought
itself); every file under `src/eventic/` and `src/tests/`; `pyproject.toml`,
`dbos-config.yaml`, `alembic.ini`, both migrations.

**Baseline:** `.venv/bin/python -m pytest src/tests -q` → **28 passed in 34.9s**
(≈1.25s/test — the slowness is itself a data point, see probe_03). Deps: dbos
2.29.0, sqlalchemy 2.0.51, pydantic 2.11.7, fastapi 0.115.13, alembic 1.19.0.

**Probes written & run (all against the repo `.venv`, SQLite):**

- `probe_01_durable_v0_and_amplification.py` — **[verified]**
  - Constructing `Story(...)` writes a `records` row *immediately* (1 INSERT).
  - v0's `version_id` is **random uuid4**; only mutation version_ids are the
    deterministic uuid5. The README's "deterministic for every mutation" claim
    has an unadvertised v0 exception.
  - A 4-touch edit session (construct + 2 field sets + 1 `props.add`) → **4 full-row
    INSERTs**, each preceded by a full-model re-validation.
  - The L4 no-op guard avoids the write but *still* constructs+validates a whole
    new object to discover the no-op.
- `probe_02_concurrency_silent_lost_update.py` — **[verified]**
  - Two readers of v0 edit **disjoint fields**; the second write derives the same
    `(id, version=1)`, is dropped by `ON CONFLICT DO NOTHING`, and **vanishes with
    no error**. The losing in-memory object still reports the value it "wrote."
    The README sells this mechanism as a correctness feature; in the standalone
    path it is a silent lost-update with no SERIALIZABLE retry to save it.
- `probe_03_dbos_lifecycle_cost.py` — **[verified]**
  - init 17ms + launch 130ms + **destroy/reset ~979ms** ≈ **1.1s per process/test**
    of pure DBOS lifecycle; the actual value delivered (versioned write + read) is
    **13.8ms**. DBOS creates **11 system tables** next to eventic's single
    `records` table. This is what makes the suite take 35s and what every plain
    script pays for.
- `probe_04_pickle_queue_args.py` — **[verified]**
  - DBOS 2.29 default serializer is **`py_pickle`** (base64+pickle). A whole
    `Record` serializes to 504 b64 chars as a queue arg. A crafted arg executes
    code on `deserialize()` — RCE if any trust boundary reaches an enqueue arg or
    the DBOS system DB.

**Decisions reached this session (delta from initial reading is recorded in
REIMAGINE_REVIEW.md §1):**

1. The *storage kernel* (append-only single table; `id` + `version`;
   deterministic mutation `version_id`; JSONB `data`) is sound — keep it.
2. Everything wrapped *around* it fights the goal: copy-on-write-on-`__setattr__`,
   durable-v0-at-construction, the `RecordMeta` metaclass + per-class queue +
   `@evented`, the `PropertiesBase._owner` back-pointer, the `Eventic(DBOS)`
   subclass/singleton, and — decisively — **DBOS as a mandatory substrate**.
3. Verdict: **thin rewrite**. Rebuild the public API around explicit
   `save()/update()/commit()`, fold properties into normal fields, and demote
   DBOS to an **optional adapter** (`eventic[dbos]`). Full argument, incl. the
   honest "just delete the library" counter, in REIMAGINE_REVIEW.md §3.

**Deliverables produced this session:** `REIMAGINE_REVIEW.md`,
`TARGET_ARCHITECTURE.md`, `REIMPLEMENTATION_PLAN.md`, `probes/` (01–04), this log.

**Open / next session:** prototype Step 1–2 of the plan (pure `Record`, `connect()`,
explicit `save/update`) on this branch and confirm the suite stays green at each
step; add a Postgres-marked concurrency test that asserts the *loud* IntegrityError
behaviour the rewrite introduces.

## 2026-08-04 — Session 2 (design dialogue → conceptual docs)

Explored, in conversation, three architectural questions and captured the durable
conclusions as two new design documents:

- Should this be built on **SQLModel**? Conclusion: only if the product pivots to
  typed-column tables — which trades away the cheap append-only versioning that
  defines eventic and raises (not lowers) the metaclass machinery the review told us
  to cut. Keep the JSONB kernel; SQLModel is a *persistence-seam* option, not the base.
- **DBOS as a mixin, events as the core?** Yes — this is the right decomposition:
  the version log *is* the event stream; delivery of events is a swappable backend
  (`sync` default, `durable` via DBOS). It resolves R-C4 (pre-commit events), R-S1
  (pickled Records), R-P3 (DBOS tax), and the same-name-class crash as side effects.
- **Incremental diff storage?** Doable as a *storage codec* invisible above
  `hydrate()`; composes with events + DBOS, but fights the SQLModel typed-column
  option. Recommend forward-delta + snapshot-every-K (preserves append-only);
  opt-in, off by default, only worth it for large aggregates.

**New deliverables:**
- `CONCEPT.md` — the irreducible idea (log-is-the-event-stream) and the seven
  invariants (I1–I7) + the canonical write/read pipeline every plugin extends.
- `PLUGINS.md` — a general, closed-set (five-seam) plugin framework where
  durable-delivery, diff-storage, typed-columns, multi-tenancy, encryption, etc. are
  all the same kind of thing (a provider at a seam). Includes the anti-gold-plating
  guardrails (§8) and the rule: *don't build the framework before the second real
  plugin exists.*

These two are conceptual/forward-looking; they don't change the Plan's first three
(already-green) steps. `TARGET_ARCHITECTURE.md` §1 (events) and the `eventic.dbos`
adapter should be re-expressed in plugin/seam terms on its next revision.
