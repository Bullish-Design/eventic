#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "eventic[pg]",
#     "uvicorn",
# ]
# ///

from __future__ import annotations

import os
import uvicorn
from eventic import Eventic, Record, on
from fastapi import Request
import json
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

POSTGRES_DB = os.environ["POSTGRES_DB"]
POSTGRES_USER = os.environ["POSTGRES_USER"]
POSTGRES_PASSWORD = os.environ["POSTGRES_PASSWORD"]

db_url = (
    "postgresql://"
    + POSTGRES_USER
    + ":"
    + POSTGRES_PASSWORD
    + "@postgres:5432/"
    + POSTGRES_DB
)

print(f"\nConnecting to Postgres at {db_url}\n")


# ────────────────────────────────── 3. Concrete Record ─────────────────────────────────
class Story(Record):
    title: str | None = None
    body: str | None = None

    # @property
    def _format_story(self) -> str:
        return f"\nTitle: {self.title}\n\n  {self.body}\n\n"


def main() -> None:
    """Minimal Eventic server."""
    # db_url = db_url  # os.getenv("DBOS_DATABASE_URL")
    if not db_url:
        raise ValueError("DBOS_DATABASE_URL required")

    # Create minimal FastAPI app with Eventic
    app = Eventic.create_app("eventic-server", db_url=db_url)

    @app.get("/")
    def health() -> dict[str, str]:
        return {"status": "running"}

    @app.post("/webhook")
    async def webhook(request: Request):
        """Log any incoming JSON to file."""
        try:
            # Get request body
            body = await request.json()

            # Create an instance of Story:
            story = Story(**body)

            # Add metadata
            log_entry = {
                "timestamp": datetime.utcnow().isoformat(),
                "headers": dict(request.headers),
                "source_ip": request.client.host if request.client else None,
                "body": body,
                "story": story,
            }

            ## Append to JSONL file
            # with open(LOG_FILE, "a") as f:
            #    f.write(json.dumps(log_entry) + "\n")
            print(f"{json.dumps(log_entry, indent=2)}\n")
            print(f"✓ Logged webhook from {log_entry['source_ip']}")

            return {
                "status": "logged",
                "Request:": log_entry,
            }

        except Exception as e:
            print(f"✗ Error: {e}")
            return {"status": "error", "message": str(e)}, 400

    # Run server
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
