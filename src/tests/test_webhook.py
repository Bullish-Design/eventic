"""Webhook regression test (M6/L10): strict input schema, no metadata injection.

Runs last (alphabetically) because importing ``eventic.main`` creates a
second Eventic instance and a ``WebhookStory`` class — DBOS's global registry
is shared per process, so this test isolates itself with its own sqlite DB
(via DBOS_DATABASE_URL) and resets the singleton afterwards.
"""

import uuid

from fastapi.testclient import TestClient

from eventic import Eventic


def test_webhook_persists_and_rejects_metadata_injection(tmp_path, monkeypatch):
    monkeypatch.setenv("DBOS_DATABASE_URL", f"sqlite:///{tmp_path / 'webhook.db'}")

    import eventic.main as main_module  # noqa: PLC0415 (import after env set)

    with TestClient(main_module.app) as client:
        resp = client.post(
            "/webhook",
            json={
                "title": "hello",
                "body": "world",
                "version": 99,  # reserved — must be rejected/ignored
                "id": str(uuid.uuid4()),  # reserved
                "version_id": str(uuid.uuid4()),  # reserved
                "properties": {"status": "injected"},  # reserved
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "logged"

    story = main_module.WebhookStory.hydrate(uuid.UUID(data["id"]))
    assert story.title == "hello"
    assert story.body == "world"
    assert story.version == 0  # the posted version=99 was ignored (M6)
    assert "status" not in story.properties.list()  # properties not injectable (M6)

    Eventic.reset()  # leave a clean singleton for any later test
