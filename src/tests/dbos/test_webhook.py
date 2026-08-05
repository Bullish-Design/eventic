"""Webhook end-to-end test (Step 9) — the opt-in DBOS path.

Post → persisted v0 → durable reindex on the queue with an id-only arg (R-S1);
metadata injection still rejected (M6). The reindex wait must happen *inside*
the TestClient window — closing it triggers the DBOS lifespan shutdown, which
stops the queue workers. Imports ``eventic.examples.webhook`` inside the test
(after the env is set) exactly like the old suite did, and destroys DBOS
afterwards so later tests re-init cleanly.
"""

import time
import uuid

import pytest
from sqlalchemy import text

from eventic.connect import _reset, engine

try:
    from dbos import DBOS

    HAVE_DBOS = True
except ImportError:
    HAVE_DBOS = False

pytestmark = pytest.mark.skipif(not HAVE_DBOS, reason="requires eventic[dbos]")


def test_webhook_persists_v0_and_durable_reindex(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DBOS_DATABASE_URL", f"sqlite:///{tmp_path / 'webhook.db'}")

    import eventic.examples.webhook as webhook_module  # noqa: PLC0415

    try:
        with TestClient(webhook_module.app) as client:
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

            note = webhook_module.Note.get(rid)
            assert note.title == "hello" and note.body == "world"
            assert note.version == 0  # the posted version=99 was ignored (M6)
            assert note.meta == {}  # no metadata injection (M6)

            # durable reindex eventually runs on the queue with an id-only arg
            def _reindexed():
                with engine().connect() as c:
                    row = c.execute(
                        text(
                            "SELECT status FROM workflow_status "
                            "WHERE queue_name = 'notes' ORDER BY created_at DESC LIMIT 1"
                        )
                    ).scalar()
                return row == "SUCCESS"

            t0 = time.time()
            while not _reindexed() and time.time() - t0 < 20:
                time.sleep(0.3)
            assert _reindexed(), "durable reindex step never completed"
    finally:
        DBOS.destroy()
        _reset()
