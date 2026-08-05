"""The 0.2 canary that must never regress: 8+ threads racing one
``(stream, id, revision)`` produce exactly 1 winner and 7 loud errors.

F7 (006 review): this used to build ``SQLite(...)`` only, so the canary —
the one test that distinguishes the two backends' concurrency models — never
ran on Postgres. It is now parameterised over a store factory and runs on
SQLite always, Postgres when ``EVENTIC_PG_URL`` is set, under both encodings.

The ``other:`` outcome is what catches an F2 regression: a lost race that
surfaces as ``StoreError`` (unmapped constraint violation) instead of
``RevisionConflict`` fails the assertion.
"""

from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest

from eventic.encodings import get_encoding
from eventic.errors import RevisionConflict
from eventic.jsonx import canonical_bytes, digest
from eventic.sql.store import Postgres, SQLite
from eventic.wire import CommitRequest

AID = uuid.UUID(int=1)
THREADS = 8
PG_URL = os.environ.get("EVENTIC_PG_URL")


def _live_postgres() -> bool:
    if not PG_URL:
        return False
    try:
        from sqlalchemy import create_engine

        engine = create_engine(PG_URL)
        with engine.connect():
            pass
        engine.dispose()
        return True
    except Exception:  # noqa: BLE001
        return False


BACKENDS = ["sqlite", "postgres"] if _live_postgres() else ["sqlite"]


def _sqlite(tmp_path: Path, *, delta: bool) -> SQLite:
    encodings = {"todos": get_encoding("delta/1")} if delta else None
    return SQLite(str(tmp_path / f"race-{uuid.uuid4().hex}.db"), encodings=encodings)


def _postgres(*, delta: bool) -> Postgres:
    from sqlalchemy import text

    from eventic.sql.tables import metadata

    encodings = {"todos": get_encoding("delta/1")} if delta else None
    store = Postgres(PG_URL, encodings=encodings)
    with store.engine.begin() as conn:
        metadata.drop_all(conn)
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    metadata.create_all(store.engine)
    return store


def _make(backend: str, tmp_path: Path, *, delta: bool) -> SQLite | Postgres:
    if backend == "sqlite":
        return _sqlite(tmp_path, delta=delta)
    return _postgres(delta=delta)


def _seed(store: Any) -> None:
    payload = canonical_bytes({"text": "seed", "done": False})
    store.commit(
        [
            CommitRequest(
                stream="todos",
                aggregate_id=AID,
                expected_revision=None,
                kind="create",
                schema_version=1,
                payload=payload,
                digest=digest(payload),
                meta=canonical_bytes({}),
                meta_version=1,
                fingerprint="f",
            )
        ]
    )


def _race(store: Any, *, expected_revision: int) -> tuple[int, int]:
    barrier = threading.Barrier(THREADS)
    results: list[str] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        barrier.wait()
        payload = canonical_bytes({"text": f"w{i}", "done": False})
        try:
            store.commit(
                [
                    CommitRequest(
                        stream="todos",
                        aggregate_id=AID,
                        expected_revision=expected_revision,
                        kind="change",
                        schema_version=1,
                        payload=payload,
                        digest=digest(payload),
                        meta=canonical_bytes({}),
                        meta_version=1,
                        fingerprint="f",
                    )
                ]
            )
            with lock:
                results.append("ok")
        except RevisionConflict:
            with lock:
                results.append("conflict")
        except Exception as exc:  # noqa: BLE001
            with lock:
                results.append(f"other:{type(exc).__name__}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "a racer hung"
    ok = results.count("ok")
    conflict = results.count("conflict")
    other = [r for r in results if r != "ok" and r != "conflict"]
    assert not other, other
    return ok, conflict


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("delta", [False, True])
def test_eight_threads_race_one_revision_one_winner(
    tmp_path: Path, backend: str, delta: bool
) -> None:
    store = _make(backend, tmp_path, delta=delta)
    try:
        _seed(store)
        ok, conflict = _race(store, expected_revision=0)
        assert ok == 1, f"winners: {ok}"
        assert conflict == THREADS - 1
    finally:
        store.close()


@pytest.mark.parametrize("backend", BACKENDS)
def test_winner_is_durable_and_losers_are_loud(tmp_path: Path, backend: str) -> None:
    from eventic.ids import AggregateKey

    store = _make(backend, tmp_path, delta=False)
    try:
        _seed(store)
        ok, conflict = _race(store, expected_revision=0)
        assert ok == 1
        assert conflict == THREADS - 1
        head = store.head(AggregateKey("todos", AID))
        assert head is not None
        assert head.revision == 1  # exactly one revision appended
        assert head.payload["text"].startswith("w")
    finally:
        store.close()


def test_concurrent_create_of_new_aggregate_has_one_winner(
    tmp_path: Path,
) -> None:
    """The create-create race has no head row to lock (F2's gap): the unique
    constraint is the backstop and must surface as RevisionConflict."""
    store = _make("sqlite", tmp_path, delta=False)
    try:
        barrier = threading.Barrier(THREADS)
        results: list[str] = []
        lock = threading.Lock()

        def creator(i: int) -> None:
            barrier.wait()
            payload = canonical_bytes({"text": f"c{i}", "done": False})
            try:
                store.commit(
                    [
                        CommitRequest(
                            stream="todos",
                            aggregate_id=AID,
                            expected_revision=None,
                            kind="create",
                            schema_version=1,
                            payload=payload,
                            digest=digest(payload),
                            meta=canonical_bytes({}),
                            meta_version=1,
                            fingerprint="f",
                        )
                    ]
                )
                with lock:
                    results.append("ok")
            except RevisionConflict:
                with lock:
                    results.append("conflict")
            except Exception as exc:  # noqa: BLE001
                with lock:
                    results.append(f"other:{type(exc).__name__}")

        threads = [threading.Thread(target=creator, args=(i,)) for i in range(THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not any(t.is_alive() for t in threads), "a creator hung"
        assert results.count("ok") == 1, results
        assert results.count("conflict") == THREADS - 1, results
        assert not [r for r in results if r not in ("ok", "conflict")], results
        from eventic.ids import AggregateKey

        head = store.head(AggregateKey("todos", AID))
        assert head is not None
        assert head.revision == 0
    finally:
        store.close()
