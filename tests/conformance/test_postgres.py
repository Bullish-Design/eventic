"""Phase 9: PostgreSQL passes the identical store conformance suite.

These run only against a live Postgres (CI service); they skip locally. The
schema-parity gate (create_all vs alembic upgrade head) is here too.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import create_engine, inspect

from eventic.sql import Postgres
from eventic.sql.tables import metadata
from eventic.testing.runner import run_all, summary

PG_URL = os.environ.get("EVENTIC_PG_URL")


def _live_postgres() -> bool:
    if not PG_URL:
        return False
    try:
        engine = create_engine(PG_URL)
        with engine.connect():
            pass
        engine.dispose()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _live_postgres(), reason="requires a live Postgres (CI service)"
)


def _drop_everything(engine: Any) -> None:
    """Drop the eventic tables and alembic's own version table.

    ``metadata.drop_all`` leaves ``alembic_version`` behind, so an alembic
    run from an earlier test would otherwise persist its stamp into the next
    scenario's "fresh" database.
    """
    from sqlalchemy import text

    with engine.begin() as conn:
        metadata.drop_all(conn)
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


def _pg_factory() -> Callable[[], Postgres]:
    """A store factory giving every scenario a clean database.

    Scenarios share fixed aggregate UUIDs, so each one must run against a
    fresh schema — exactly what the SQLite conformance test gets with a new
    file per scenario.
    """

    def factory() -> Postgres:
        store = Postgres(PG_URL)
        _drop_everything(store.engine)
        metadata.create_all(store.engine)
        return store

    return factory


def test_store_conformance_on_postgres() -> None:
    stores: list[Postgres] = []
    factory = _pg_factory()

    def tracked() -> Postgres:
        store = factory()
        stores.append(store)
        return store

    try:
        results = run_all(tracked)
    finally:
        for store in stores:
            store.close()
    failed = [r for r in results if not r.passed]
    assert not failed, summary(results)


def test_schema_parity_create_all_vs_alembic() -> None:
    assert PG_URL
    engine = create_engine(PG_URL)
    _drop_everything(engine)
    with engine.begin():
        metadata.create_all(engine)
    created_tables = set(inspect(engine).get_table_names())

    import importlib.resources as resources

    from alembic import command
    from alembic.config import Config

    pkg = resources.files("eventic.sql.migrations")
    cfg = Config(str(pkg / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", PG_URL)
    _drop_everything(engine)
    command.upgrade(cfg, "head")
    migrated_tables = set(inspect(engine).get_table_names())
    engine.dispose()

    # create_all cannot create alembic_version (it is alembic bookkeeping, not
    # part of the eventic metadata), so compare only the eventic tables.
    assert created_tables - {"alembic_version"} == migrated_tables - {"alembic_version"}


def test_alembic_check_clean_on_create_all_database() -> None:
    assert PG_URL
    engine = create_engine(PG_URL)
    with engine.begin():
        metadata.drop_all(engine)
        metadata.create_all(engine)

    import importlib.resources as resources

    from alembic import command
    from alembic.config import Config

    pkg = resources.files("eventic.sql.migrations")
    cfg = Config(str(pkg / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", PG_URL)
    command.check(cfg)
    engine.dispose()


def test_jsonb_does_not_break_replay() -> None:
    """Replay detection compares digests, never the JSONB round trip."""
    import uuid

    from eventic.ids import AggregateKey
    from eventic.jsonx import canonical_bytes, digest
    from eventic.wire import CommitRequest

    store = Postgres(PG_URL)
    from eventic.sql.tables import metadata as md

    md.drop_all(store.engine)
    md.create_all(store.engine)
    try:
        payload = canonical_bytes({"n": 1.0})
        request = CommitRequest(
            stream="todos",
            aggregate_id=uuid.UUID(int=1),
            expected_revision=None,
            kind="create",
            schema_version=1,
            payload=payload,
            digest=digest(payload),
            meta=canonical_bytes({}),
            meta_version=1,
            fingerprint="f",
        )
        store.commit([request])
        # JSONB will render 1.0 as 1; a replayed write must still be detected
        replay = CommitRequest(
            stream="todos",
            aggregate_id=uuid.UUID(int=1),
            expected_revision=None,
            kind="create",
            schema_version=1,
            payload=payload,
            digest=digest(payload),
            meta=canonical_bytes({}),
            meta_version=1,
            fingerprint="f",
        )
        result = store.commit([replay])[0]
        assert result.replayed is True
        head = store.head(AggregateKey("todos", uuid.UUID(int=1)))
        assert head is not None
        assert head.digest == digest(payload)
    finally:
        store.close()
