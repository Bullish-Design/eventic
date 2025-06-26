"""
full_demo.py – One-shot showcase of Eventic + DBOS queues.

Assumes:
  • DBOS >= 1.5  (Queue API present)
  • Postgres URL in $DBOS_DATABASE_URL
"""

import os
import uuid
from pprint import pprint

from fastapi import FastAPI
from sqlalchemy import create_engine
# from dbos import DBOS, DBOSConfig, Queue

from eventic import PropertiesBase, Record, Eventic
# from eventic.runtime import Eventic

# ────────────────────────────────── 1. DBOS + FastAPI ──────────────────────────────────

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

print(f"\nConnecting to Postgres at {db_url}\n")


app = Eventic.create_app("eventic-demo", db_url=db_url)


# ────────────────────────────────── 3. Concrete Record ─────────────────────────────────
class Story(Record):
    title: str | None = None
    body: str | None = None


# Access the auto-generated queue (defined by RecordMeta → queue_story)
story_queue = Eventic.queue(Story._queue_name)  # type: ignore[attr-defined]


# ────────────────────────────────── 4. DBOS steps ──────────────────────────────────────
@Eventic.transaction()
def create_story() -> uuid.UUID:
    s0 = Story()  # version 0
    print(f"\n→ Created Story v{s0.version} / id={s0.id} / version_id={s0.version_id}")
    Story._store.append(s0)
    return s0.id


@Eventic.transaction()
def draft(story_id: uuid.UUID, text: str) -> None:
    s = Story.hydrate(story_id)
    s.body = text  # version bump + queued event


@Eventic.transaction()
def change_title(story_id: uuid.UUID, new_title: str) -> None:
    """Bumps version and writes an immutable row."""
    s = Story.hydrate(story_id)
    s.title = new_title


@Eventic.transaction()
def publish(story_id: uuid.UUID) -> None:
    s = Story.hydrate(story_id)
    # s.title = title  # version bump
    # s.properties.add(status="published")

    # ---- add published flag *and* ensure a new version is persisted ----
    props = s.properties  # same object, but we'll re‑assign below
    props.add(status="published")
    s.properties = props


@Eventic.step()
def snapshot(story_id: uuid.UUID):
    latest = Story.hydrate(story_id)
    print(f"\n→ Snapshot @ v{latest.version} / version_id={latest.version_id}")
    pprint(latest.model_dump(), width=80)


@Eventic.transaction()
def add_property(story_id: uuid.UUID, key: str, value: str) -> None:
    """Add an arbitrary key/value pair to the Story.properties bag."""
    s = Story.hydrate(story_id)
    s.properties.add(**{key: value})
    s.properties = s.properties  # trigger version bump


@Eventic.transaction()
def tag_extra(story_id: uuid.UUID, **kv) -> None:
    """Generic helper that adds an arbitrary property (creates new version)."""
    s = Story.hydrate(story_id)
    props = s.properties
    props.add(**kv)
    s.properties = props


# ────────────────────────────────── 5. Workflow that drives everything ────────────────
@app.get("/")
@Eventic.workflow()
def end_to_end_demo() -> dict:
    sid = create_story()
    story_queue.enqueue(snapshot, sid)

    # enqueue heavy steps on the per-class queue
    story_queue.enqueue(draft, sid, "Once upon a time…")
    story_queue.enqueue(snapshot, sid)

    story_queue.enqueue(change_title, sid, "The Eventic Tale")
    story_queue.enqueue(snapshot, sid)

    story_queue.enqueue(publish, sid)
    story_queue.enqueue(snapshot, sid)

    story_queue.enqueue(add_property, sid, "audience", "kids")
    story_queue.enqueue(snapshot, sid)

    story_queue.enqueue(tag_extra, sid, reviewed=True)  # <- final property
    # story_queue.enqueue(snapshot, sid)

    # Wait until queue tasks finish (simple polling)
    handle = story_queue.enqueue(snapshot, sid)
    handle.get_result()  # blocks until the final snapshot prints

    versions = Story.hydrate(sid).version + 1
    found_published = Story._store.find_by_properties({"status": "published"})
    found_audience = Story._store.find_by_properties({"audience": "kids"})
    return {
        "id": str(sid),
        "versions": versions,
        "search_found_published": str(found_published),
        "search_found_audience": str(found_audience),
    }


# ────────────────────────────────── 6. Run worker & fire the workflow ────────────────
if __name__ == "__main__":
    # from dbos import DBOSRunner
    Eventic.init(name="eventic-demo", database_url=db_url)
    Eventic.launch()  # starts DBOS worker threads
    # DBOS().run_in_background()  # starts worker threads locally
    result = end_to_end_demo()
    print("\nJSON response that an HTTP caller would receive:")
    pprint(result, width=80)
