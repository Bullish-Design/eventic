"""The 0.2 canary that must never regress: 8+ threads racing one
``(stream, id, revision)`` produce exactly 1 winner and 7 loud errors."""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

from eventic.encodings import get_encoding
from eventic.errors import RevisionConflict
from eventic.jsonx import canonical_bytes, digest
from eventic.sql.store import SQLite
from eventic.wire import CommitRequest

AID = uuid.UUID(int=1)
THREADS = 8


def _make_store(tmp_path: Path, *, delta: bool = False) -> SQLite:
    encodings = {"todos": get_encoding("delta/1")} if delta else None
    return SQLite(str(tmp_path / "race.db"), encodings=encodings)


def _seed(store: SQLite) -> None:
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


def _race(store: SQLite, *, expected_revision: int) -> tuple[int, int]:
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


def test_eight_threads_race_one_revision_one_winner(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    try:
        _seed(store)
        ok, conflict = _race(store, expected_revision=0)
        assert ok == 1, f"winners: {ok}"
        assert conflict == THREADS - 1
    finally:
        store.close()


def test_eight_threads_race_one_revision_one_winner_under_delta(
    tmp_path: Path,
) -> None:
    store = _make_store(tmp_path, delta=True)
    try:
        _seed(store)
        ok, conflict = _race(store, expected_revision=0)
        assert ok == 1, f"winners: {ok}"
        assert conflict == THREADS - 1
    finally:
        store.close()


def test_winner_is_durable_and_losers_are_loud(tmp_path: Path) -> None:
    from eventic.ids import AggregateKey

    store = _make_store(tmp_path)
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
