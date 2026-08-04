"""Regression tests for the P0/P1 data-integrity findings (C1, C4, C5).

These reproduce the failures documented in REVIEW.md — they are expected to
fail on the pre-refactor code and pass from Step 3 onward.
"""

import uuid

from eventic import Eventic, Record


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
    """C4: where() must return hydrated records, not bare UUIDs."""

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
