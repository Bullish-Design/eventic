#!/usr/bin/env python3
"""Eventic webhook server.

Run with ``uvicorn eventic.main:app`` (see dbos-config.yaml). The database
URL comes from ``DBOS_DATABASE_URL`` when set, otherwise from ``POSTGRES_*``
env vars (docker-compose style).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel

from eventic import Eventic, Record

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


class WebhookStory(Record):
    """Story-like log entry for the webhook.

    Named uniquely (not Story) because DBOS's queue registry is keyed by the
    derived queue name (queue_story) — two same-named Record classes cannot
    coexist in one process.
    """

    title: str | None = None
    body: str | None = None

    def _format_story(self) -> str:
        return f"\nTitle: {self.title}\n\n  {self.body}\n\n"


class WebhookPayload(BaseModel):
    """Strict input schema.

    NO version/id/version_id/properties fields — they are aggregate-managed
    and must never come from a client (M6).
    """

    title: str | None = None
    body: str | None = None


def build_app(*, db_url: str | None = None) -> FastAPI:
    """Create the FastAPI app wired to a fresh Eventic instance."""
    app = Eventic.create_app("eventic-server", db_url=db_url or _default_db_url())

    @app.post("/webhook")
    async def webhook(payload: WebhookPayload):
        """Log any incoming JSON to file."""
        story = WebhookStory(
            title=payload.title, body=payload.body
        )  # v0 auto-persisted (C5)
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "body": payload.model_dump(),
            "story": story.model_dump_json(),
        }
        print(json.dumps(log_entry))
        return {"status": "logged", "id": str(story.id)}

    return app


app = build_app()  # module-level object for `uvicorn eventic.main:app`


def main() -> None:
    """Run the server (dev convenience; uvicorn is the normal entry point)."""
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
