"""Eventic webhook server — the opt-in DBOS path.

An incoming POST is persisted as version 0, then durable delivery is handed to
DBOS: the commit stages an outbox row, and the request handler drains it onto a
DBOS queue, where each handler runs as a DBOS step with the full ``Event``.

The app is built by a **function you call** — importing this module has no
side effects and connects nothing (F20). Requires ``pip install eventic[dbos]``.
Run with uvicorn:
    uvicorn eventic.examples.webhook:build_app --factory
"""

from __future__ import annotations

import json
import os

from dotenv import load_dotenv
from pydantic import BaseModel

from eventic import Record, on_commit
from eventic.contrib.dbos import DbosDispatcher, DbosStore

load_dotenv()


def _default_db_url() -> str:
    if os.environ.get("DBOS_DATABASE_URL"):
        return os.environ["DBOS_DATABASE_URL"]
    return (
        "postgresql://"
        + os.environ.get("POSTGRES_USER", "")
        + ":"
        + os.environ.get("POSTGRES_PASSWORD", "")
        + "@"
        + os.environ.get("POSTGRES_HOST", "postgres")
        + ":"
        + os.environ.get("POSTGRES_PORT", "5432")
        + "/"
        + os.environ.get("POSTGRES_DB", "eventic")
    )


class Note(Record):
    """Versioned aggregate — construction is pure, save() persists."""

    title: str | None = None
    body: str | None = None


class NoteIn(BaseModel):
    """Strict input schema — NO id/version/version_id/meta fields (M6)."""

    title: str | None = None
    body: str | None = None


@on_commit(Note, via="outbox", queue="notes")
def reindex(event) -> None:
    """DBOS step: rebuild the Event, re-hydrate, index. Idempotent by contract."""
    note = event.record
    print(json.dumps({"reindexed": str(note.id), "title": note.title, "version": note.version}))


def build_app(*, db_url: str | None = None):
    """FastAPI + DBOS + an eventic ``DbosStore`` on one database."""
    from fastapi import FastAPI

    from dbos import DBOS

    url = db_url or _default_db_url()
    app = FastAPI()
    DBOS(config={"name": "notes-svc", "application_database_url": url}, fastapi=app)
    store = DbosStore(url, create_tables=True).activate()

    @app.post("/webhook")
    async def webhook(payload: NoteIn):
        note = Note(title=payload.title, body=payload.body).save()
        DbosDispatcher(store).drain()  # durable delivery onto the DBOS queue
        return {"status": "logged", "id": str(note.id)}

    return app
