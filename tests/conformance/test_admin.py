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
