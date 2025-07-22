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
from eventic import Eventic

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

    # Run server
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
