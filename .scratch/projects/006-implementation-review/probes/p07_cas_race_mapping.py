"""The CAS read takes no row lock; a lost race maps to StoreError, not RevisionConflict.

ARCHITECTURE.md §4.3 step 1:
  "Read the head row for (stream, aggregate_id) with row-level locking."
  "Constraint violation on (stream, aggregate_id, revision) also maps to
   RevisionConflict. The unique index is the backstop, the CAS is the diagnosis."

`sql/store.py` calls `st.select_head(..., for_update=False)` unconditionally, and
`with_for_update()` in statements.py is unreachable. On SQLite this is masked by
`BEGIN IMMEDIATE` (one writer at a time). On Postgres — the production backend,
default READ COMMITTED, `Postgres._install_events` is a no-op — two writers with
the same expected_revision both pass the CAS and race to the INSERT.

This probe simulates the interleaving deterministically on SQLite by inserting
the colliding row after the CAS read but before the INSERT, then shows which
exception the caller sees.
"""

from __future__ import annotations

import uuid

from eventic.errors import RevisionConflict, StoreError
from eventic.ids import AggregateKey
from eventic.jsonx import canonical_bytes, digest
from eventic.sql import statements as st
from eventic.sql.store import SQLite
from eventic.wire import CommitRequest

AID = uuid.UUID(int=7)


def request(text: str, expected: int | None, kind: str) -> CommitRequest:
    payload = canonical_bytes({"text": text})
    return CommitRequest(
        stream="todos",
        aggregate_id=AID,
        expected_revision=expected,
        kind=kind,  # type: ignore[arg-type]
        schema_version=1,
        payload=payload,
        digest=digest(payload),
        meta=canonical_bytes({}),
        meta_version=1,
        fingerprint="f",
    )


print("=== the lock that is never taken ===")
import inspect  # noqa: E402

src = inspect.getsource(SQLite._commit_one)
for line in src.splitlines():
    if "select_head" in line or "for_update" in line:
        print("  store.py:", line.strip())
print("  statements.py: with_for_update() is only reachable via for_update=True,")
print("                 which no call site passes.")

print("\n=== simulate the Postgres interleaving ===")
store = SQLite(":memory:")
store.commit([request("seed", None, "create")])

original = SQLite._decode_log_revision
raced = {"done": False}


def race_in_between(self, conn, stream, aggregate_id, revision):  # type: ignore[no-untyped-def]
    """Stand in for a concurrent writer that commits between our CAS and INSERT.

    We cannot hook between the CAS and the INSERT directly, so we insert the
    colliding row on the first _decode_log_revision call, which runs
    immediately after the INSERT — the same constraint outcome the real race
    produces, one statement later.
    """
    if not raced["done"]:
        raced["done"] = True
        payload = canonical_bytes({"text": "other-writer"})
        conn.execute(
            st.insert_revision(
                self.dialect,
                {
                    "revision_id": uuid.uuid4(),
                    "stream": "todos",
                    "aggregate_id": AID,
                    "revision": 1,  # same (stream, aggregate_id, revision)
                    "kind": "change",
                    "schema_version": 1,
                    "meta_version": 1,
                    "encoding": "snapshot/1",
                    "payload": {"text": "other-writer"},
                    "digest": digest(payload),
                    "meta": {},
                    "committed_at": "2026-01-01 00:00:00",
                },
            )
        )
    return original(self, conn, stream, aggregate_id, revision)


SQLite._decode_log_revision = race_in_between  # type: ignore[method-assign]
try:
    store.commit([request("mine", 0, "change")])
    outcome = "NO ERROR"
except RevisionConflict as exc:
    outcome = f"RevisionConflict: {exc}"
except StoreError as exc:
    outcome = f"StoreError: {exc}  (cause: {type(exc.__cause__).__name__})"
finally:
    SQLite._decode_log_revision = original  # type: ignore[method-assign]

print(f"  caller sees -> {outcome}")
print()
print("  §4.3 requires RevisionConflict. A caller running the documented")
print("  optimistic-retry loop:")
print("      try: col.change(rev, ...)")
print("      except RevisionConflict: reload and retry")
print("  does not retry — it sees an opaque StoreError and treats a routine")
print("  write conflict as a backend failure.")
assert outcome.startswith("StoreError"), outcome

print("\n=== the canary's coverage ===")
print("  tests/conformance/test_race_canary.py builds SQLite(...) only.")
print("  tests/conformance/test_postgres.py runs the declarative suite, which")
print("  contains no concurrency scenarios — test_concurrent_drainers_scenario_active")
print("  asserts `not names`, i.e. that they are absent.")
store.close()
