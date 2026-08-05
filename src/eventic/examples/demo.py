"""eventic demo — versioned Pydantic aggregates whose history is an event
stream. Core only: pydantic + SQLAlchemy on SQLite, no DBOS required.

Run:  .venv/bin/python -m eventic.examples.demo
"""

from __future__ import annotations

import tempfile

from eventic import Record, connect, on_commit


class Story(Record):
    title: str | None = None
    body: str | None = None


@on_commit(Story, kind="create")
def log_new_story(event):
    print(f"  [event] created {event.record.title!r} (v{event.record.version})")


@on_commit(Story, kind="update")
def log_update(event):
    print(f"  [event] updated -> {event.delta}")


def main() -> None:
    connect(f"sqlite:///{tempfile.gettempdir()}/eventic_demo.db")

    print("== construct (pure, no I/O) ==")
    story = Story(title="The Eventic Tale", body="Once upon a time…")
    print(f"  in-memory v{story.version} / version_id={story.version_id}")

    print("\n== save() — the only way to persist ==")
    story = story.save()

    print("\n== update() returns the NEW version; the original is untouched ==")
    story = story.update(body="Once upon a time… the log became the event stream.")
    print(f"  now v{story.version}")

    print("\n== draft().commit() batches several changes into ONE version ==")
    d = story.draft()
    d.meta["status"] = "published"
    d.meta["audience"] = "developers"
    story = d.commit()  # assignment is the point — commit RETURNS the new version
    print(f"  now v{story.version}; history length={len(Story.history(story.id))}")

    print("\n== reads ==")
    fresh = Story.get(story.id)
    print(f"  latest : v{fresh.version} {fresh.title!r} status={fresh.meta['status']}")
    v1 = Story.get(story.id, version=1)
    print(f"  v1     : {v1.body[:48]}…")
    published = Story.where(**{"meta.status": "published"})
    print(f"  where(meta.status=published) -> {len(published)} record(s)")

    print("\n== the full version log ==")
    for v in Story.history(story.id):
        print(f"  v{v.version}: title={v.title!r} body={v.body[:40]!r}…")

    print("\ndone — demo ran end-to-end.")


if __name__ == "__main__":
    main()
