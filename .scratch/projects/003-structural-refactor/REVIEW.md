# Eventic 0.2 — Adversarial Review (verified)

Evidence base for `CONCEPT.md` (v3) and `IMPLEMENTATION_GUIDE.md`. Every finding below
was **reproduced against the shipped code**, not inferred by reading. Reproductions
are in `probes/`; run them with `.venv/bin/python probes/<file>`.

**Baseline at review time:** `86 passed, 1 skipped` — the suite misses every finding
below. Commit `77084af` (`main`).

**What is genuinely good and must survive the refactor:** the append-only kernel, the
deterministic `uuid5` identity rule, and the optimistic-lock. `probe_06` races 8
threads at one `(id, version)` and gets exactly **1 winner and 7 loud
`StaleVersionError`s**. I5 is correct. Do not regress it.

---

## Tier 1 — Correctness and data integrity

### F1 · Plugin mixins inject phantom fields that are persisted forever
`plugins/__init__.py:43-47` · `probe_03`

`Plugin` annotates `seam`, `provides`, `requires`, `priority`, `mode`. Pydantic's
metaclass walks the MRO for annotations, so `class Doc(Record, DiffStorage)` turns all
five into **model fields**, written into `data` on every commit:

```json
{"kind":"snapshot","state":{"seam":"codec","provides":["codec"],
 "requires":["persistence:json"],"priority":0,"mode":null,"body":"hello"}}
```

`Doc.where(seam="codec")` returns records. Both README flagship examples are affected.
Framework metadata in the user's domain state, in an append-only log you cannot rewrite.

### F2 · Subclassing a plugin-bearing Record installs a Record as the codec
`record.py:80` · `probe_02`

`__init_subclass__` scans `cls.__bases__` for `issubclass(Plugin)`. For
`class SubDoc(Doc)`, the base `Doc` *is* a Plugin subclass (it inherited from
`DiffStorage`) with `seam = CODEC`:

```
type(Sub2._codec) : <class '__main__.Doc2'>     # a Record instance as the codec
Sub2.__eventic_plugins__: [<class '__main__.Doc2'>]
```

If the parent has any required field, **class definition raises `ValidationError`**.
You cannot subclass a plugin-bearing Record.

### F3 · I7 violated — events fire before durability on the DBOS path
`persistence.py:77-87` + `pipeline.py:95-98` · `probe_04`

In the ambient-session path the row is not durable when the pipeline emits:

```
handler fired while tx still open: [99]
rows visible to another connection: 0
rows after ROLLBACK: 0
>>> I7 VIOLATED: handler ran for a version that was never durable
```

`DurableEvents` is safe (outbox); `SyncDelivery` — the **default** — is not. Any
`eventic[dbos]` app with a sync handler emits phantom events on workflow abort. This
is the headline invariant, broken on the path the DBOS extra exists to serve.

### F4 · `DiffStorage` cannot express a field removal — ghosts resurrect
`codec.py:51,55` · `probe_05`

`_field_diff` emits only keys present in `after`; `_apply_patch` is `dict.update`,
which can never delete:

```
v0 stored with tag: True
v1 in memory has tag: False
v1 read back has tag: True   <-- GHOST
```

Silent divergence between what you wrote and what you read, in the plugin the docs
call "the second real plugin that retroactively justifies the framework."

### F5 · `created_ts` is always `None`
`record.py:129` + `codec.py:33` · `probe_01`

The value is popped so the column default stamps it, but hydration returns
`rows[-1].data`, which holds the `null` captured at encode time:

```
hydrated created_ts: None
DB column created_ts: 2026-08-05 04:13:56.038250
```

A documented public field that never works. `test_record_pure.py:59` asserts
`is None` — the test enshrines the bug.

### F6 · `edit()` strands you on a stale handle — visible in the shipped demo
`record.py:143` · `probe_01`

`self.update(**changes)` is called and the result discarded. The library's own demo:

```
== edit() batches several changes into ONE version ==
  now v1; history length=3        <-- head is actually v2
```

The next write from that handle dies with `StaleVersionError`.

### F7 · `SingleTableJSONB.query()` raises `NameError`
`persistence.py:211` — uses `_jsonable`; line 209 imports only `_match`.
`query() -> NameError: name '_jsonable' is not defined`.

---

## Tier 2 — Architecture

### F8 · `use()` is a no-op
`plugins/__init__.py:92-98` · `probe_01`. `_GLOBAL_PLUGINS` is appended to and **never
read by anything**. It is exported in `__all__`. `test_public_surface.py:20` imports it
and asserts nothing about it — exactly how this survived.

### F9 · The identity seam is dead — four seams, not five
`identity.py:29`. `version_id_for` is never called; `record.py:108,128` call the module
function directly. A custom identity plugin is assembled, attached, and ignored.

### F10 · The delivery seam is a process-global switch
`plugins/__init__.py:57` + `pipeline.py:97` · `probe_02`

```
class OptedIn(Record, Spy): ...
class NotOptedIn(Record): ...   # never mentions Spy
Spy saw: ['OptedIn', 'NotOptedIn']
```

The `requires` gate is enforced per-class while the effect is global, so the capability
check is decorative. Merely `import eventic.dbos` (line 123) flips durable delivery on
process-wide.

### F11 · `before_commit`'s return value is discarded
`interceptor.py:27` documents "inspect/**enrich** … return record"; `pipeline.py:73`
throws the result away. Verified in `probe_04`. Asymmetric with `after_hydrate`, which
*is* threaded (`pipeline.py:104`).

### F12 · Dead API surface
`contribute_schema` (`interceptor.py:37`) never called · `full_state_rows`
(`codec.py:26,77`) never read · `Veto` exported from neither `eventic` nor
`eventic.plugins` · `TypedTable` a stub whose only purpose is passing one guardrail
test.

### F13 · Two incompatible identity models
Events key on the **class object** (`events.py:38`, docstring: "same-named classes
never cross-fire"); persistence keys on **`cls.__name__`** (`pipeline.py:85`). Same-named
classes in different modules silently share a log. Polymorphic reads are impossible.

### F14 · `extra="allow"` contradicts the thesis
`record.py:61`. A library selling "conflicts are loud, never silent" silently accepts
and permanently persists `Todo(txt="typo")`.

### F15 · `get()` raises bare `KeyError`
`pipeline.py:113`, against `errors.py:12` ("All library-raised exceptions derive from
`EventicError`"). Verified in `probe_01`.

---

## Tier 3 — Performance

### F16 · `where()` is N+1 with no SQL pushdown
`pipeline.py:126-136` · `probe_04`. Loads every latest row, decodes in Python, re-reads
each match. **12 SQL statements for 10 aggregates × 10 versions.** On JSONB, pushing
down zero predicates.

### F17 · Delta point reads stream the entire history
`codec.py:98` · `probe_06`. `fetch` calls `stream()` — all rows — then slices back to
the nearest snapshot. At 800 versions with `K=20`: **18 ms per `get()`**, ~40× the rows
needed.

### F18 · Index set is backwards
`models.py:32-37`. `UniqueConstraint(id, version)` + `Index(id, version)` + `id
index=True` — three overlapping indexes on one prefix. Every query filters on
`class_type`, which has **no index**.

### F19 · `history()` is O(N²) in Python
`pipeline.py:121-123` builds `rows[:i+1]` for every `i`, but `FullSnapshot.decode` reads
only `rows[-1]`. 0.04 s at 1200 versions — cleanliness, not urgency.

---

## Tier 4 — Hygiene

| # | Finding |
|---|---|
| F20 | `examples/webhook.py:78` — `app = build_app()` constructs DBOS and connects to Postgres **at import**, in a library that elsewhere insists nothing happens at import time. |
| F21 | `persistence.py:35` — `Callable` never imported; only `from __future__ import annotations` hides it. |
| F22 | `events.py:74` — `_HANDLER_IDS.setdefault` silently keeps the first of two functions sharing a `module:qualname`; durable dispatch then runs the wrong one. |
| F23 | `connect.py:30` — `create_tables=True` by default, against "Alembic is the source of truth in production". |

---

## Root causes

Twenty-three findings, three causes:

1. **Plugins selected by inheritance** → F1, F2, and the reason the seam model could
   not express per-class delivery (F10).
2. **Six module-level mutable globals** (`_ENGINE`, `_DELIVERY_MODES`,
   `_GLOBAL_PLUGINS`, `_HANDLERS`, `_ambient_session`, `_QUEUES`), each with a
   `_reset_*` hook → F8, F10, and the ambient-session hack behind F3.
3. **Nothing owns the transaction** → F3, and the absence of a natural home for the
   outbox and the head projection (F16, F17).

`CONCEPT.md` §11 maps each to its structural fix; `IMPLEMENTATION_GUIDE.md` sequences
them so the load-bearing one lands first.
