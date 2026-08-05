"""Benchmarks: commit throughput, point reads, history, search, drain.

Run against SQLite locally; against live Postgres in CI. Prints a table the
report in ``docs/BENCHMARKS.md`` can be regenerated from.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from eventic.ids import AggregateKey
from eventic.jsonx import canonical_bytes, digest
from eventic.sql import Postgres, SQLite
from eventic.wire import CommitRequest

AID = uuid.UUID(int=42)


def _make_store(tmp: Path):
    url = os.environ.get("EVENTIC_PG_URL")
    if url:
        store = Postgres(url)
        from eventic.sql.tables import metadata

        metadata.drop_all(store.engine)
        metadata.create_all(store.engine)
        return store, "postgresql"
    return SQLite(str(tmp / "bench.db")), "sqlite"


def _request(i: int) -> CommitRequest:
    payload = canonical_bytes({"text": f"row-{i}", "done": bool(i % 2), "n": i})
    return CommitRequest(
        stream="todos",
        aggregate_id=AID,
        expected_revision=None if i == 0 else i - 1,
        kind="create" if i == 0 else "change",
        schema_version=1,
        payload=payload,
        digest=digest(payload),
        meta=canonical_bytes({}),
        meta_version=1,
        fingerprint="f",
    )


def _bench(fn, n: int) -> float:
    start = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - start) / n


def main() -> None:
    tmp = Path("/tmp")
    store, backend = _make_store(tmp)
    key = AggregateKey("todos", AID)

    n_commit = 100
    start = time.perf_counter()
    for i in range(n_commit):
        store.commit([_request(i)])
    commit_s = (time.perf_counter() - start) / n_commit

    # 10k-head search corpus
    for i in range(10_000):
        payload = canonical_bytes({"text": f"s{i}", "done": False, "bucket": i % 10})
        store.commit(
            [
                CommitRequest(
                    stream="bucket",
                    aggregate_id=uuid.UUID(int=1000 + i),
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

    point_ms = _bench(lambda: store.revision(key, n_commit - 1), 200) * 1000
    head_ms = _bench(lambda: store.head(key), 200) * 1000
    history_ms = _bench(lambda: store.history(key, after=-1, limit=100), 50) * 1000
    where_ms = (
        _bench(
            lambda: store.search("bucket", {"bucket": 3}, cursor=None, limit=100), 50
        )
        * 1000
    )
    print(f"backend: {backend}")
    print(f"commit (ms/op): {commit_s * 1000:.3f}")
    print(f"point read rev {n_commit - 1} (ms): {point_ms:.3f}")
    print(f"head read (ms): {head_ms:.3f}")
    print(f"history limit=100 (ms): {history_ms:.3f}")
    print(f"where bucket=3 over 10k heads (ms): {where_ms:.3f}")
    store.close()


if __name__ == "__main__":
    main()
