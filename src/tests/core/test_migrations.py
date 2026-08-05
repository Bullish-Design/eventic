"""Migration tests (Step 22): the 0.2 → 0.3 triad rebuild.

A realistic 0.2 database (both codecs, a plugin-bearing class with phantom
fields) is seeded via the old migration chain, then ``alembic upgrade head``
rebuilds it and the upgraded data reads back correctly through the 0.3 API.
"""

import json
import uuid
from datetime import datetime, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from eventic import Delta, Record, connect


def _cfg(url):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


class Story(Record, stream="Story"):
    title: str | None = None
    body: str | None = None


def _seed_02_database(url):
    """Build the 0.1→0.2 chain (up to the fold) and seed old-shape rows:
    a FullSnapshot row with phantom plugin keys and managed keys, plus a
    DiffStorage snapshot+delta pair. UUIDs are stored in the format the 0.2
    ORM used (32-hex on SQLite, hyphenated on Postgres), so a raw insert
    matches what the ORM binds."""
    command.upgrade(_cfg(url), "fold_properties_into_data")

    eng = create_engine(url)
    is_pg = eng.dialect.name == "postgresql"

    def _id_str(u):
        return str(u) if is_pg else u.hex

    rid = uuid.uuid4()
    created = datetime.now(timezone.utc).isoformat()
    with Session(eng) as s:
        # FullSnapshot row — the model dump with managed + phantom keys
        s.execute(
            text(
                "INSERT INTO records (version_id, id, version, class_type, "
                "created_ts, data) VALUES "
                "(:version_id, :id, :version, :class_type, :created_ts, :data)"
            ),
            {
                "version_id": _id_str(uuid.uuid5(uuid.NAMESPACE_URL, f"eventic:{rid}:0")),
                "id": _id_str(rid),
                "version": 0,
                "class_type": "Story",
                "created_ts": created,
                "data": json.dumps(
                    {
                        "id": str(rid),
                        "version": 0,
                        "version_id": str(uuid.uuid4()),
                        "created_ts": None,
                        "seam": "codec",
                        "provides": ["codec"],
                        "requires": ["persistence:json"],
                        "priority": 0,
                        "mode": None,
                        "meta": {"record_type": "Story", "status": "published"},
                        "title": "old story",
                        "body": "old body",
                    }
                ),
            },
        )
        # DiffStorage snapshot + delta pair (old shapes)
        drid = uuid.uuid4()
        s.execute(
            text(
                "INSERT INTO records (version_id, id, version, class_type, "
                "created_ts, data) VALUES "
                "(:version_id, :id, :version, :class_type, :created_ts, :data)"
            ),
            {
                "version_id": _id_str(uuid.uuid5(uuid.NAMESPACE_URL, f"eventic:{drid}:0")),
                "id": _id_str(drid),
                "version": 0,
                "class_type": "MigratedNote",
                "created_ts": created,
                "data": json.dumps(
                    {
                        "kind": "snapshot",
                        "state": {
                            "id": str(drid), "version": 0,
                            "version_id": str(uuid.uuid4()), "created_ts": None,
                            "seam": "codec", "mode": None,
                            "meta": {}, "title": "draft note", "body": "d",
                        },
                    }
                ),
            },
        )
        s.execute(
            text(
                "INSERT INTO records (version_id, id, version, class_type, "
                "created_ts, data) VALUES "
                "(:version_id, :id, :version, :class_type, :created_ts, :data)"
            ),
            {
                "version_id": _id_str(uuid.uuid5(uuid.NAMESPACE_URL, f"eventic:{drid}:1")),
                "id": _id_str(drid),
                "version": 1,
                "class_type": "MigratedNote",
                "created_ts": created,
                "data": json.dumps(
                    {"kind": "delta", "patch": {"title": "edited note", "version": 1}}
                ),
            },
        )
        s.commit()
    eng.dispose()
    return rid, drid


def test_upgrade_reads_back_through_03_api(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    rid, drid = _seed_02_database(url)

    command.upgrade(_cfg(url), "head")

    # the triad exists; records is gone
    eng = create_engine(url)
    names = set(inspect(eng).get_table_names())
    assert {"eventic_log", "eventic_head", "eventic_outbox"} <= names
    assert "records" not in names
    eng.dispose()

    connect(url)
    story = Story.get(rid)
    assert story.title == "old story"
    assert story.version == 0
    assert story.meta == {"record_type": "Story", "status": "published"}
    assert story.created_ts is not None

    # the diff-stored aggregate reads correctly after the fold (head = v1).
    # The old DiffStorage rows migrated to the NEW delta shape, so the class
    # reading that stream declares the matching codec.
    class Note(Record, stream="MigratedNote", codec=Delta(k=20)):
        title: str | None = None
        body: str | None = None

    note = Note.get(drid)
    assert note.title == "edited note"  # the delta folded into the head
    assert note.version == 1
    assert note.body == "d"
    assert len(Note.history(drid)) == 2


def test_upgrade_strips_phantom_keys_from_log(tmp_path):
    from eventic import connect
    from eventic.store import active_store
    from eventic.store.schema import LogRow
    from sqlalchemy import select

    url = f"sqlite:///{tmp_path / 'old.db'}"
    _seed_02_database(url)
    command.upgrade(_cfg(url), "head")

    connect(url)
    with Session(active_store().engine) as s:
        row = s.execute(select(LogRow).where(LogRow.stream == "Story")).scalar_one()
    assert "seam" not in row.data
    assert "provides" not in row.data
    assert "id" not in row.data and "version_id" not in row.data


def test_upgrade_downgrade_roundtrip_sqlite(tmp_path):
    url = f"sqlite:///{tmp_path / 'rt.db'}"
    _seed_02_database(url)
    command.upgrade(_cfg(url), "head")
    eng = create_engine(url)
    assert "eventic_log" in set(inspect(eng).get_table_names())
    eng.dispose()

    command.downgrade(_cfg(url), "fold_properties_into_data")
    eng2 = create_engine(url)
    assert "records" in set(inspect(eng2).get_table_names())
    with eng2.connect() as c:
        n = c.execute(text("SELECT COUNT(*) FROM records")).scalar()
    eng2.dispose()
    assert n == 3  # all three rows survived the roundtrip


@pytest.mark.postgres
def test_migrations_roundtrip_postgres():
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
            tables = {r[0] for r in c.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
            )}
        assert {"eventic_log", "eventic_head", "eventic_outbox"} <= tables
    finally:
        command.downgrade(cfg, "base")
