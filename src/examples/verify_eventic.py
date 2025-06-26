#!/usr/bin/env python
"""
eventic_example.py
==================
A FastAPI + DBOS demo that:

1. Bootstrap DBOS from the `DBOS_DATABASE_URL` env var.
2. Creates a SQLAlchemy engine and passes it to `eventic.init_eventic`.
3. Defines a `Story` record (sub-class of eventic.Record).
4. Uses DBOS transactions/workflows to create & mutate stories.
5. Exposes a `/` endpoint that runs the whole flow and returns JSON
   showing the final state and how many immutable versions were written.
"""

import os
import uuid
from typing import Dict

import uvicorn
from fastapi import FastAPI
from sqlalchemy import create_engine
from dbos import DBOS, DBOSConfig  # , transaction, workflow


# ── import your library ──────────────────────────────────────────────────────
from eventic import init_eventic, Record, PropertiesBase  # top-level exports

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
    + "@localhost/"
    + POSTGRES_DB
)

app = FastAPI()
dbos_conf: DBOSConfig = {
    "name": "eventic-demo",
    "database_url": db_url,
}
DBOS(config=dbos_conf, fastapi=app)


# ── 2. bootstrap Eventic + DBOS ──────────────────────────────────────────────
# engine = create_engine(db_url, pool_pre_ping=True, future=True)
# init_eventic(engine)


# ── 3. define a concrete Record subclass for the test ────────────────────────
class Story(Record):
    title: str | None = None
    body: str | None = None


# -----------------------------------------------------------------------------
# 4.  DBOS steps / transactions
# -----------------------------------------------------------------------------
@DBOS.transaction()
def create_story() -> uuid.UUID:
    """Create v0 and return the stable aggregate id."""
    s = Story()
    return s.id


@DBOS.transaction()
def change_title(story_id: uuid.UUID, new_title: str) -> None:
    """Bumps version and writes an immutable row."""
    s = Story.hydrate(story_id)  # latest
    s.title = new_title  # triggers copy-on-write


@DBOS.transaction()
def count_versions(story_id: uuid.UUID) -> int:
    """Return how many versions were stored so far."""
    latest = Story.hydrate(story_id)
    return latest.version + 1  # 0-based counter


# -----------------------------------------------------------------------------
# 5.  Workflow exposed via HTTP
# -----------------------------------------------------------------------------
@app.get("/")
@DBOS.workflow()
def demo_flow() -> Dict[str, str]:
    sid = create_story()
    change_title(sid, "Draft title")
    change_title(sid, "Final title")
    versions = count_versions(sid)

    final_state = Story.hydrate(sid)
    return {
        "aggregate_id": str(sid),
        "latest_version_id": str(final_state.version_id),
        "versions": versions,
        "title": final_state.title,
        "properties": final_state.properties.list(),
    }


# -----------------------------------------------------------------------------
# 6.  Run DBOS worker & UVicorn (only in script mode)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    # in production you’d run dbos-runner separately; launch() is fine for a test
    DBOS.launch()
    uvicorn.run(app, host="0.0.0.0", port=8002, reload=False)
