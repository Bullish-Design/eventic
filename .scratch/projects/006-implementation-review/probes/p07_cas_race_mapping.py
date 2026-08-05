"""Regression probe: a lost CAS race surfaces as RevisionConflict, not StoreError.

F2 (006 review) had three parts, all in `src/eventic/sql/`:
  1. `store.py` called `select_head(..., for_update=False)` at the only CAS
     call site, so `with_for_update()` was unreachable — on Postgres two
     writers with the same expected_revision both passed the CAS.
  2. `Postgres._install_events` was `pass` — no BEGIN IMMEDIATE equivalent.
  3. `commit()` wrapped everything in `except Exception -> StoreError`; there
     was no IntegrityError arm, so the unique-index backstop could never
     produce RevisionConflict, and the documented optimistic-retry loop did
     not retry.

Fixed (007 Phase 2):
  1. The CAS read passes `for_update=True` (Postgres emits FOR UPDATE; SQLite
     ignores it). One line covers both backends.
  2. `commit()` maps a unique-constraint violation on
     `(stream, aggregate_id, revision)` — or its deterministic revision_id
     primary key — to RevisionConflict. Other constraint violations (empty
     stream name, empty intent queue, kind/revision mismatch) still surface
     as StoreError.
  3. The race canary is parameterised over a store factory and runs on
     SQLite and Postgres, under both encodings; the declarative suite gained
     capability-gated concurrency scenarios.

Run: devenv shell -- uv run python .scratch/.../probes/p07_cas_race_mapping.py
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime

from eventic.errors import RevisionConflict, StoreError
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


print("=== the lock is now taken at the CAS call site ===")
src = inspect.getsource(SQLite._commit_one)
for line in src.splitlines():
    if "select_head" in line or "for_update" in line:
        print("  store.py:", line.strip())
assert "for_update=True" in src, "the CAS read must take the row lock"
print("  -> select_head(..., for_update=True): FOR UPDATE on Postgres,")
print("     ignored on SQLite. read-path head() stays for_update=False.")

print("\n=== simulate the Postgres interleaving ===")
store = SQLite(":memory:")
store.commit([request("seed", None, "create")])

original = SQLite._decode_log_revision
raced = {"done": False}


def race_in_between(self, conn, stream, aggregate_id, revision):  # type: ignore[no-untyped-def]
    """Stand in for a concurrent writer that committed between our CAS and INSERT.

    We cannot hook between the CAS and the INSERT directly, so we insert the
    colliding row on the first _decode_log_revision call, which runs
    immediately after the INSERT — the same unique-constraint outcome the real
    race produces, one statement later.
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
                    "committed_at": datetime.now(UTC),
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
print("  §4.3 requires RevisionConflict so the documented optimistic-retry")
print("  loop reloads and retries. An opaque StoreError would treat a routine")
print("  write conflict as a backend failure.")
assert outcome.startswith("RevisionConflict"), outcome
print("\nOK: the constraint backstop surfaces as RevisionConflict.")

print("\n=== the canary's coverage ===")
print("  tests/conformance/test_race_canary.py is parameterised over a store")
print("  factory: SQLite always, Postgres when EVENTIC_PG_URL is set, under")
print("  both encodings. The declarative suite gained concurrency scenarios:")
print("  'same-expected-revision race has exactly one winner',")
print("  'concurrent create of the same aggregate has exactly one winner',")
print("  'concurrent drainers claim each intent exactly once' (capability-")
print("  gated on concurrent_drainers).")
store.close()
