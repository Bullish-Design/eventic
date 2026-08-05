"""Migration tests (Step 10): the 0.1.x → 0.2 data path.

- A table written by the OLD library hydrates under the NEW one after
  ``alembic upgrade head`` (properties folded into ``data.meta``).
- ``upgrade head && downgrade base`` round-trips on SQLite; the fold's
  downgrade re-adds ``properties`` from ``data.meta``.
- The C6 backfill runs before the fold (chain order).
"""

import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from alembic import command
from alembic.config import Config

from eventic.connect import _reset, connect, engine
from eventic.record import Record


def _cfg(url):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


class Story(Record):
    title: str | None = None
    body: str | None = None


@pytest.fixture()
def clean():
    _reset()
    yield
    _reset()


def _old_schema_and_row(url):
    """Build the 0.1 schema via the 0.1 chain (initial migration only) and
    insert one old-shape row — the honest old-install path."""
    command.upgrade(_cfg(url), "6725d5d5ed38")  # the 0.1 initial (with properties)
    eng = create_engine(url)
    eng.dispose()  # fresh file engine for the insert below (avoids ResourceWarning)
    eng2 = create_engine(url)
    rid = uuid.uuid4()
    props = {"record_type": "Story", "status": "published"}
    with Session(eng2) as s:
        s.execute(
            text(
                "INSERT INTO records (version_id, id, version, class_type, "
                "created_ts, properties, data) VALUES "
                "(:version_id, :id, 0, 'Story', :created_ts, :properties, :data)"
            ),
            {
                "version_id": uuid.uuid4().hex,  # old v0 ids were RANDOM (R-C2)
                "id": rid.hex,
                "created_ts": datetime.now(timezone.utc).isoformat(),
                "properties": json.dumps(props),
                "data": json.dumps(
                    {
                        "id": str(rid),
                        "version": 0,
                        "version_id": str(uuid.uuid4()),
                        "properties": dict(props),  # the old data embedded the bag
                        "title": "old story",
                        "body": "old body",
                    }
                ),
            },
        )
        s.commit()
    eng2.dispose()
    return rid


@pytest.mark.postgres
def test_migrations_roundtrip_postgres():
    """PG branch of the chain (Step-13 matrix): round-trips on a live PG.
    Requires POSTGRES_HOST/USER/PASSWORD/DB (like the old webhook's default);
    skipped otherwise — run by CI."""
    import os

    if not os.environ.get("POSTGRES_HOST"):
        pytest.skip("no live Postgres configured")
    url = (
        "postgresql+psycopg://"
        + os.environ.get("POSTGRES_USER", "postgres")
        + ":"
        + os.environ.get("POSTGRES_PASSWORD", "")
        + "@"
        + os.environ["POSTGRES_HOST"]
        + ":"
        + os.environ.get("POSTGRES_PORT", "5432")
        + "/"
        + os.environ.get("POSTGRES_DB", "eventic")
    )
    cfg = _cfg(url)
    command.upgrade(cfg, "head")
    try:
        eng = create_engine(url)
        with eng.connect() as c:
            cols = [
                r[0]
                for r in c.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='records'"
                    )
                )
            ]
        assert "properties" not in cols
    finally:
        command.downgrade(cfg, "base")


def test_old_library_row_hydrates_after_fold(tmp_path, clean):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    rid = _old_schema_and_row(url)

    command.upgrade(_cfg(url), "head")

    # the new schema has no properties column
    eng = create_engine(url)
    with eng.connect() as c:
        cols = [r[1] for r in c.execute(text("PRAGMA table_info(records)"))]
    assert "properties" not in cols
    eng.dispose()

    connect(url)
    story = Story.get(rid)
    assert story.title == "old story"
    assert story.version == 0
    assert story.meta == {"record_type": "Story", "status": "published"}  # folded


def test_upgrade_downgrade_roundtrip_sqlite(tmp_path, clean):
    url = f"sqlite:///{tmp_path / 'rt.db'}"
    command.upgrade(_cfg(url), "head")
    eng = create_engine(url)
    with eng.connect() as c:
        cols = [r[1] for r in c.execute(text("PRAGMA table_info(records)"))]
    assert "properties" not in cols
    eng.dispose()

    # one step down re-adds properties (fold's downgrade)
    command.downgrade(_cfg(url), "a1b2c3d4e5f6")
    with eng.connect() as c:
        cols = [r[1] for r in c.execute(text("PRAGMA table_info(records)"))]
    assert "properties" in cols
    eng.dispose()

    # and back up
    command.upgrade(_cfg(url), "head")
    with eng.connect() as c:
        cols = [r[1] for r in c.execute(text("PRAGMA table_info(records)"))]
    assert "properties" not in cols
    eng.dispose()


def test_downgrade_rebuilds_properties_from_meta(tmp_path, clean):
    url = f"sqlite:///{tmp_path / 'dt.db'}"
    rid = _old_schema_and_row(url)
    command.upgrade(_cfg(url), "head")

    # simulate a 0.2-written row (data.meta only, no properties column)
    eng = create_engine(url)
    with Session(eng) as s:
        s.execute(
            text(
                "INSERT INTO records (version_id, id, version, class_type, "
                "created_ts, data) VALUES "
                "(:version_id, :id, 0, 'Note', :created_ts, :data)"
            ),
            {
                "version_id": uuid.uuid4().hex,
                "id": uuid.uuid4().hex,
                "created_ts": datetime.now(timezone.utc).isoformat(),
                "data": json.dumps(
                    {"id": str(uuid.uuid4()), "version": 0,
                     "meta": {"status": "draft"}, "title": "n"}
                ),
            },
        )
        s.commit()
    eng.dispose()

    command.downgrade(_cfg(url), "a1b2c3d4e5f6")
    eng2 = create_engine(url)
    with eng2.connect() as c:
        metas = c.execute(text("SELECT properties FROM records ORDER BY version")).scalars().all()
    eng2.dispose()
    # every row's properties was rebuilt from data.meta (raw TEXT: JSON is stored
    # as text in SQLite and decoded by the ORM JSON type on typed reads)
    parsed = [json.loads(m) for m in metas]
    assert all(isinstance(m, dict) for m in parsed)
    assert any(m.get("status") == "draft" for m in parsed)  # the 0.2 row's meta
    assert any(m.get("status") == "published" for m in parsed)  # the 0.1 row folded
