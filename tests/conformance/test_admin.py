"""Phase 9/11: StoreAdmin — rebuild, verify, schema check on SQLite."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from pydantic import BaseModel

from eventic.app import App
from eventic.errors import StoreError
from eventic.jsonx import canonical_bytes, digest
from eventic.sql.admin import SqlAdmin
from eventic.sql.store import SQLite
from eventic.stream import Stream
from eventic.wire import CommitRequest


class Todo(BaseModel):
    text: str
    done: bool = False


def _seed(tmp_path: Path, writes: int = 5) -> tuple[SQLite, App]:
    store = SQLite(str(tmp_path / "admin.db"))
    todos = Stream(Todo, name="todos")
    app = App(id="demo", streams=[todos])
    aid = uuid.UUID(int=42)
    for i in range(writes):
        payload = canonical_bytes({"text": f"t{i}", "done": bool(i % 2)})
        store.commit(
            [
                CommitRequest(
                    stream="todos",
                    aggregate_id=aid,
                    expected_revision=None if i == 0 else i - 1,
                    kind="create" if i == 0 else "change",
                    schema_version=1,
                    payload=payload,
                    digest=digest(payload),
                    meta=canonical_bytes({}),
                    meta_version=1,
                    fingerprint=app.streams[0].fingerprint,
                )
            ]
        )
    return store, app


def test_rebuild_heads_byte_exact(tmp_path: Path) -> None:
    store, app = _seed(tmp_path)
    admin = SqlAdmin(store)
    report = admin.rebuild_heads(None, chunk=2)
    assert report.rebuilt == 1
    assert report.orphans_removed == 0
    assert report.mismatches == 0
    # rebuild again is idempotent
    report2 = admin.rebuild_heads(None, chunk=2)
    assert report2.rebuilt == 1
    assert report2.mismatches == 0
    store.close()


def test_rebuild_removes_orphan_heads(tmp_path: Path) -> None:
    store, app = _seed(tmp_path)
    from sqlalchemy import text

    with store.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO eventic_head (stream, aggregate_id, revision, "
                "revision_id, schema_version, meta_version, state, digest, "
                "meta, committed_at) VALUES "
                "('ghost', '00000000-0000-0000-0000-000000000099', "
                "0, '00000000-0000-0000-0000-000000000099', 1, 1, '{}', "
                "'x', '{}', CURRENT_TIMESTAMP)"
            )
        )
    admin = SqlAdmin(store)
    report = admin.rebuild_heads(None, chunk=2)
    assert report.orphans_removed == 1
    store.close()


def test_verify_clean(tmp_path: Path) -> None:
    store, app = _seed(tmp_path)
    report = SqlAdmin(store).verify(None, chunk=2)
    assert report.revisions_checked == 5
    assert report.mismatches == 0
    store.close()


def test_verify_detects_corruption(tmp_path: Path) -> None:
    store, app = _seed(tmp_path)
    from sqlalchemy import text

    with store.engine.begin() as conn:
        conn.execute(
            text(
                'UPDATE eventic_revision SET payload = \'{"text":"corrupted"}\' '
                "WHERE revision = 1"
            )
        )
    report = SqlAdmin(store).verify(None, chunk=2)
    assert report.mismatches >= 1
    store.close()


def test_schema_check_clean_and_drift(tmp_path: Path) -> None:
    store, app = _seed(tmp_path)
    admin = SqlAdmin(store)
    report = admin.check(app)
    assert not report.drift
    assert report.streams[0][4] is True
    # simulate a model change without a schema_version bump
    from sqlalchemy import text

    with store.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE eventic_schema SET fingerprint = 'wrong' WHERE stream = 'todos'"
            )
        )
    report2 = admin.check(app)
    assert report2.drift
    assert report2.streams[0][4] is False
    store.close()


def test_schema_check_seeds_missing_ledger(tmp_path: Path) -> None:
    store, app = _seed(tmp_path, writes=0)
    admin = SqlAdmin(store)
    report = admin.check(app)
    assert not report.drift
    assert report.streams[0][4] is True
    store.close()


def test_migrate_requires_alembic_or_works() -> None:
    store = SQLite(":memory:")
    admin = SqlAdmin(store)
    try:
        admin.migrate()  # alembic installed -> creates schema
    except StoreError:
        pytest.fail("alembic is installed in this environment")
    from eventic.ids import AggregateKey

    assert store.head(AggregateKey("todos", uuid.UUID(int=1))) is None
    store.close()


def _seed_many(
    tmp_path: Path, writes_per_aggregate: int = 1, aggregates: int = 300
) -> None:
    """Many small aggregates, one row each, plus a handful of longer ones."""
    store = SQLite(str(tmp_path / "many.db"))
    todos = Stream(Todo, name="todos")
    app = App(id="demo", streams=[todos])
    for n in range(aggregates):
        payload = canonical_bytes({"text": "x" * 300, "done": False})
        store.commit(
            [
                CommitRequest(
                    stream="todos",
                    aggregate_id=uuid.UUID(int=n + 1),
                    expected_revision=None,
                    kind="create",
                    schema_version=1,
                    payload=payload,
                    digest=digest(payload),
                    meta=canonical_bytes({}),
                    meta_version=1,
                    fingerprint=app.streams[0].fingerprint,
                )
            ]
        )
    return store


def test_verify_memory_bounded_per_chunk_not_per_aggregate(tmp_path: Path) -> None:
    """F5 (Option A): verify's peak memory tracks the chunk size, not the
    aggregate count times document size."""
    import tracemalloc

    store = _seed_many(tmp_path, aggregates=400)
    admin = SqlAdmin(store)

    def peak(chunk: int) -> float:
        tracemalloc.start()
        admin.verify(None, chunk=chunk)
        _, peak_kib = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return peak_kib / 1024

    small_chunk = peak(10)
    large_chunk = peak(400)
    # memory scales with chunk (the operator's knob) but a chunk of 10 must
    # stay far below materializing all 400 documents
    assert small_chunk < large_chunk
    assert small_chunk < 1024, f"verify peak at chunk=10 is {small_chunk:.0f} KiB"
    store.close()


def test_list_intents_respects_limit_and_cursor_roundtrips(tmp_path: Path) -> None:
    """F5: list_intents pages by limit, and the opaque cursor continues where
    the previous page stopped with no overlap and no gap."""
    import uuid as _uuid

    from eventic.sql.tables import eventic_intent as intents_t

    store = _seed_many(tmp_path, aggregates=1)
    admin = SqlAdmin(store)
    # insert intents directly (ordering is by created_at, intent_id)
    from datetime import UTC, datetime

    base = datetime.now(UTC)
    with store.engine.begin() as conn:
        for i in range(7):
            conn.execute(
                intents_t.insert().values(
                    intent_id=_uuid.uuid5(_uuid.NAMESPACE_URL, f"intent-{i}"),
                    subscription_id=f"sub.{i}",
                    revision_id=_uuid.uuid4(),
                    queue="q",
                    status="pending",
                    attempts=0,
                    available_at=base,
                    created_at=base.replace(second=base.second + i),
                )
            )

    rows, cursor = admin.list_intents(limit=3)
    assert len(rows) == 3
    assert cursor is not None
    seen = [row["intent_id"] for row in rows]

    rows2, cursor2 = admin.list_intents(limit=3, cursor=cursor)
    assert len(rows2) == 3
    assert cursor2 is not None
    seen += [row["intent_id"] for row in rows2]
    assert len(set(seen)) == 6, "pages must not overlap"

    rows3, cursor3 = admin.list_intents(limit=3, cursor=cursor2)
    assert len(rows3) == 1
    assert cursor3 is None
    seen += [row["intent_id"] for row in rows3]
    assert len(set(seen)) == 7, "pages must not skip rows"
    assert sorted(str(i) for i in seen) == sorted(
        str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"intent-{i}")) for i in range(7)
    )
    store.close()
