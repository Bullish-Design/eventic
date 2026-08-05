"""Webhook end-to-end (F20): the app is built by a function, not at import.

POST → persisted v0 → outbox staged → the request handler (a DBOS workflow)
drains it onto a DBOS queue → the reindex handler runs as a DBOS step with the
full Event. Metadata injection is still rejected (M6).
"""

import time
import uuid

import pytest
from sqlalchemy import text

from eventic import Record, on_commit
from eventic.store.schema import LogRow

try:
    from dbos import DBOS

    HAVE_DBOS = True
except ImportError:
    HAVE_DBOS = False

pytestmark = pytest.mark.skipif(not HAVE_DBOS, reason="requires eventic[dbos]")


def test_webhook_persists_v0_and_durable_reindex(tmp_path, monkeypatch):
    from sqlalchemy import select, func
    from sqlalchemy.orm import Session

    from fastapi.testclient import TestClient

    url = f"sqlite:///{tmp_path / 'webhook.db'}"
    monkeypatch.setenv("DBOS_DATABASE_URL", url)

    import eventic.examples.webhook as wh  # importing must NOT build an app

    assert not hasattr(wh, "app")  # F20
    app = wh.build_app()

    try:
        with TestClient(app) as client:
            resp = client.post(
                "/webhook",
                json={
                    "title": "hello",
                    "body": "world",
                    "version": 99,  # reserved — must be ignored (M6)
                    "id": str(uuid.uuid4()),
                    "version_id": str(uuid.uuid4()),
                    "meta": {"status": "injected"},
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "logged"
            rid = uuid.UUID(data["id"])

            note = wh.Note.get(rid)
            assert note.title == "hello" and note.body == "world"
            assert note.version == 0  # the posted version=99 was ignored (M6)
            assert note.meta == {}  # no metadata injection (M6)

            from eventic import active_store

            with Session(active_store().engine) as s:
                kind = s.execute(
                    select(LogRow.kind).where(LogRow.id == rid)
                ).scalar_one()
                assert kind == "create"

            # the durable reindex eventually runs on the DBOS queue
            def _reindexed():
                from eventic import active_store

                with active_store().engine.connect() as c:
                    row = c.execute(
                        text(
                            "SELECT status FROM workflow_status "
                            "WHERE queue_name = 'notes' ORDER BY created_at DESC LIMIT 1"
                        )
                    ).scalar()
                return row == "SUCCESS"

            t0 = time.time()
            while not _reindexed() and time.time() - t0 < 25:
                time.sleep(0.3)
            assert _reindexed(), "durable reindex step never completed"
    finally:
        DBOS.destroy()
