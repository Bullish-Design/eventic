"""Regression tests for the P0/P1 data-integrity findings (C1, C4, C5).

These reproduce the failures documented in REVIEW.md — they are expected to
fail on the pre-refactor code and pass from Step 3 onward.
"""

import uuid

from eventic import Eventic, Record, on


class Story(Record):
    title: str | None = None
    body: str | None = None


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
