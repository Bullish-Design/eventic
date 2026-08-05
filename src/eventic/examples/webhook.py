"""Eventic webhook server — the opt-in DBOS path.

An incoming POST is persisted as version 0, then a *durable* reindex step is
enqueued with only the record **id** (never a pickled Record — R-S1). The
reindex runs later on the DBOS queue worker, re-hydrates by id, and must be
idempotent (the async contract).

Requires ``pip install eventic[dbos]``. Run with uvicorn:
    uvicorn eventic.examples.webhook:app
"""

from __future__ import annotations

import json
import os
import uuid

from dotenv import load_dotenv
from pydantic import BaseModel

from eventic import Record
from eventic.dbos import create_app, durable, queue

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


@durable
def reindex(note_id: str) -> None:
    """DBOS step: id in, re-hydrate, index. Idempotent by contract."""
    note = Note.get(uuid.UUID(note_id))
    print(json.dumps({"reindexed": str(note.id), "title": note.title, "version": note.version}))


def build_app(*, db_url: str | None = None):
    """FastAPI + DBOS + the eventic engine on one database."""
    app = create_app("notes-svc", db_url=db_url or _default_db_url())

    @app.post("/webhook")
    async def webhook(payload: NoteIn):
        note = Note(title=payload.title, body=payload.body).save()
        queue("notes").enqueue(reindex, str(note.id))  # id-only arg (R-S1)
        return {"status": "logged", "id": str(note.id)}

    return app


app = build_app()  # module-level object for `uvicorn eventic.examples.webhook:app`
