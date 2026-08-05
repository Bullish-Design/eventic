"""Regression tests for the P0/P1 data-integrity findings (C1, C4, C5).

These reproduce the failures documented in REVIEW.md — they are expected to
fail on the pre-refactor code and pass from Step 3 onward.
"""

import uuid

import pytest

from eventic import Eventic, Record, on


class Story(Record):
    title: str | None = None
    body: str | None = None


class Note(Record):
    text: str | None = None


def test_mutation_outside_transaction_persists(eventic):
    """C1: mutating a record outside any DBOS transaction must persist."""
    s = Story(title="Once upon a time")
    s.body = "a new body"

    fresh = Story.hydrate(s.id)
    assert fresh.id == s.id
    assert fresh.body == "a new body"
    assert fresh.version == 1


def test_v0_row_persisted_on_construction(eventic):
    """C5: constructing a record must persist version 0 immediately."""
    s = Story(title="hello")

    fresh = Story.hydrate(s.id)
    assert fresh.id == s.id
    assert fresh.title == "hello"
    assert fresh.version == 0


def test_where_returns_records(eventic):
    """C4: where() must return hydrated records, not bare UUIDs.

    Still failing until Step 4.2 (find_by_properties rewrite) — the
    pre-fix query uses Postgres-only DISTINCT ON and non-JSON `contains`.
    """

    @Eventic.transaction()
    def seed() -> uuid.UUID:
        s1 = Story(title="a")
        s1.properties.add(status="published")
        s1.properties = s1.properties  # version bump → persisted
        return s1.id

    sid = seed()
    results = Story.where(status="published")

    assert len(results) == 1
    assert isinstance(results[0], Record)
    assert results[0].id == sid


def test_read_your_own_write_inside_transaction(eventic):
    """H2: inside one DBOS transaction, reads must see the transaction's own writes."""

    @Eventic.transaction()
    def mutate_and_read():
        s = Story(title="t0")
        s.body = "t1"
        fresh = Story.hydrate(s.id)  # same transaction → sees own writes
        return fresh.body, fresh.version

    body, version = mutate_and_read()
    assert body == "t1"
    assert version == 1


def test_construct_then_hydrate_roundtrip(eventic):
    """C5: v0 row persisted on construction survives a hydrate round-trip."""
    s = Story(title="roundtrip", body="x")
    fresh = Story.hydrate(s.id)
    assert fresh.model_dump(mode="json") == s.model_dump(mode="json")
    assert fresh.version == s.version == 0


def test_create_event_fires_after_persist(eventic):
    """H6 timing: a create handler must be able to hydrate the new record."""
    created = []

    @on.create(Story)
    def handler(story):
        fresh = Story.hydrate(story.id)  # v0 already persisted
        created.append((story.id, fresh.title))

    s = Story(title="evented")
    assert len(created) == 1
    assert created[0][0] == s.id
    assert created[0][1] == "evented"


def test_replayed_append_is_idempotent(eventic):
    """C6: re-appending the same (id, version) row is a no-op, not a duplicate."""
    s = Story(title="t")
    s.body = "x"  # v1 — deterministic version_id derived from (id, 1)

    fresh = Story.hydrate(s.id)  # v1 record
    # Simulate a crash-recovery replay: append v1's exact row again.
    replay = Story.model_validate(fresh.model_dump(mode="json"))
    replay._store.append(replay)

    # read version attributes while the stream session is open (rows are
    # detached once the generator closes)
    versions = [r.version for r in Story._store.stream(s.id)]
    assert versions == [0, 1]  # still exactly v0 + v1


def test_concurrent_mutations_do_not_duplicate_versions(eventic):
    """C6: two writers deriving v1 from the same v0 must not create duplicate versions.

    Both compute the same deterministic version_id for (id, 1); the second
    insert is ignored, so the history stays exactly [v0, v1].
    """
    s = Story(title="base")  # v0
    a = Story.hydrate(s.id)  # writer A's view of v0
    b = Story.hydrate(s.id)  # writer B's view of v0

    a.title = "from A"  # both derive version 1 from the same base...
    b.title = "from B"  # ...same (id, version=1), same deterministic version_id

    versions = [r.version for r in Story._store.stream(s.id)]
    assert versions == [0, 1]  # exactly one v1 — no duplicate versions
    fresh = Story.hydrate(s.id)
    assert fresh.title in ("from A", "from B")  # one writer won


def test_where_filters_by_class_type(eventic):
    """H4: identical properties on two classes must not cross-fire."""

    @Eventic.transaction()
    def seed():
        s = Story(title="s")
        s.properties.add(status="published")
        s.properties = s.properties
        n = Note(text="n")
        n.properties.add(status="published")
        n.properties = n.properties
        return s.id, n.id

    sid, nid = seed()
    assert [r.id for r in Story.where(status="published")] == [sid]
    assert [r.id for r in Note.where(status="published")] == [nid]


def test_hydrate_wrong_class_raises(eventic):
    """H4: hydrating a row under the wrong class raises KeyError."""
    s = Story(title="t")
    with pytest.raises(KeyError):
        Note.hydrate(s.id)


def test_hydrate_at_version(eventic):
    """H8: at_version selects the newest row with version <= at_version."""
    s = Story(title="v0")
    s.body = "v1"
    s.title = "v2"

    v0 = Story.hydrate(s.id, at_version=0)
    v1 = Story.hydrate(s.id, at_version=1)
    v2 = Story.hydrate(s.id, at_version=2)
    latest = Story.hydrate(s.id)

    assert v0.title == "v0" and v0.body is None and v0.version == 0
    assert v1.body == "v1" and v1.title == "v0" and v1.version == 1
    assert v2.title == "v2" and v2.body == "v1" and v2.version == 2
    assert latest.version == 2
