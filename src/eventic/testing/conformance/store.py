"""Store conformance scenarios: the §4 contract as declarative data.

A scenario is a frozen object: a name, a required capability set, and a
sequence of steps. The runner executes steps against a store and asserts the
expected outcomes. There are no ``assert`` statements inside this data.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from eventic.jsonx import JsonObject, JsonValue, canonical_bytes, digest
from eventic.wire import (
    IntentRequest,
)

Payload = JsonObject


def payload(obj: Payload) -> bytes:
    return canonical_bytes(obj)


def digest_of(obj: Payload) -> str:
    return digest(canonical_bytes(obj))


def meta_bytes(obj: Payload) -> bytes:
    return canonical_bytes(obj)


@dataclass(frozen=True, slots=True)
class Step:
    name: str


@dataclass(frozen=True, slots=True)
class Commit(Step):
    stream: str
    aggregate_id: UUID
    expected_revision: int | None
    kind: str
    schema_version: int
    payload: bytes
    digest: str
    meta: bytes
    meta_version: int
    fingerprint: str = ""
    intents: tuple[IntentRequest, ...] = ()
    expect_revision: int | None = None
    expect_replayed: bool | None = None
    expect_error: str | None = None


@dataclass(frozen=True, slots=True)
class Batch(Step):
    """One ``store.commit`` call with several requests, all-or-nothing."""

    commits: tuple[Commit, ...]
    expect_error: str | None = None


@dataclass(frozen=True, slots=True)
class Head(Step):
    stream: str
    aggregate_id: UUID
    expect_missing: bool = False
    expect_revision: int | None = None
    expect_digest: str | None = None
    expect_payload: Payload | None = None


@dataclass(frozen=True, slots=True)
class Exact(Step):
    stream: str
    aggregate_id: UUID
    revision: int
    expect_missing: bool = False
    expect_digest: str | None = None
    expect_payload: Payload | None = None


@dataclass(frozen=True, slots=True)
class History(Step):
    stream: str
    aggregate_id: UUID
    after: int = -1
    limit: int = 100
    expect_revisions: tuple[int, ...] = ()
    expect_payloads: tuple[Payload, ...] = ()
    expect_cursor_none: bool | None = None


@dataclass(frozen=True, slots=True)
class Search(Step):
    stream: str
    filters: Mapping[str, JsonValue]
    limit: int = 100
    cursor: str | None = None
    expect_ids: tuple[UUID, ...] = ()
    expect_payloads: tuple[Payload, ...] = ()
    expect_cursor_none: bool | None = None


@dataclass(frozen=True, slots=True)
class Claim(Step):
    queue: str
    limit: int = 10
    lease: timedelta = timedelta(seconds=1)
    expect: tuple[
        tuple[str, UUID, int], ...
    ] = ()  # (subscription_id, revision_id, attempts)
    expect_none: bool = False


@dataclass(frozen=True, slots=True)
class Settle(Step):
    status: str  # delivered | retry | dead
    available_at: datetime | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Wait(Step):
    """Sleep so a lease expires; real time only, a hundredth of a second."""

    seconds: float


@dataclass(frozen=True, slots=True)
class Race(Step):
    """N writers race one (stream, id, expected_revision) on the same store.

    Exactly one writer wins; every other writer must get ``RevisionConflict``
    (a lost race is loud, I7). Each writer uses a distinct payload so a loser
    can never be absorbed as an identical replay. A non-conflict outcome
    (e.g. ``StoreError``) fails the step — that is what catches an F2-style
    regression in the error mapping.
    """

    stream: str
    aggregate_id: UUID
    expected_revision: int | None
    kind: str = "change"
    writers: int = 8
    schema_version: int = 1
    meta_version: int = 1
    fingerprint: str = ""


@dataclass(frozen=True, slots=True)
class ConcurrentDrainers(Step):
    """Several drainers claim the same queue concurrently.

    Requires the ``concurrent_drainers`` capability (row-level claim locking).
    Every claimable intent must be claimed once across all drainers: no
    intent claimed twice (which would break the lease model) and none left
    unclaimed when the total is within the drainers' combined limits.
    """

    queue: str
    drainers: int = 3
    limit: int = 1
    expect_total: int = 0
    lease: timedelta = timedelta(seconds=1)


@dataclass(frozen=True)
class Scenario:
    name: str
    requires: frozenset[str] = frozenset()
    stores: int = 1
    steps: tuple[Step, ...] = ()


# ---------------------------------------------------------------------------
# Helpers for building scenarios
# ---------------------------------------------------------------------------


def commit_step(
    stream: str,
    aggregate_id: UUID,
    expected_revision: int | None,
    kind: str,
    obj: Payload,
    *,
    meta: Payload | None = None,
    schema_version: int = 1,
    meta_version: int = 1,
    intents: tuple[IntentRequest, ...] = (),
    expect_revision: int | None = None,
    expect_replayed: bool | None = None,
    expect_error: str | None = None,
    name: str | None = None,
) -> Commit:
    return Commit(
        name=name or f"commit {kind} rev={expected_revision}",
        stream=stream,
        aggregate_id=aggregate_id,
        expected_revision=expected_revision,
        kind=kind,
        schema_version=schema_version,
        payload=payload(obj),
        digest=digest_of(obj),
        meta=meta_bytes(meta or {}),
        meta_version=meta_version,
        intents=intents,
        expect_revision=expect_revision,
        expect_replayed=expect_replayed,
        expect_error=expect_error,
    )


def intent(subscription_id: str, revision_id: UUID, queue: str = "q") -> IntentRequest:
    return IntentRequest(
        subscription_id=subscription_id, revision_id=revision_id, queue=queue
    )


def head_step(
    stream: str,
    aggregate_id: UUID,
    *,
    expect_missing: bool = False,
    expect_revision: int | None = None,
    expect_digest: str | None = None,
    expect_payload: Payload | None = None,
    name: str = "head",
) -> Head:
    return Head(
        name=name,
        stream=stream,
        aggregate_id=aggregate_id,
        expect_missing=expect_missing,
        expect_revision=expect_revision,
        expect_digest=expect_digest,
        expect_payload=expect_payload,
    )


def exact_step(
    stream: str,
    aggregate_id: UUID,
    revision: int,
    *,
    expect_missing: bool = False,
    expect_digest: str | None = None,
    expect_payload: Payload | None = None,
    name: str = "exact",
) -> Exact:
    return Exact(
        name=name,
        stream=stream,
        aggregate_id=aggregate_id,
        revision=revision,
        expect_missing=expect_missing,
        expect_digest=expect_digest,
        expect_payload=expect_payload,
    )
