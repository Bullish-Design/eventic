"""Wire types: everything that crosses the store boundary.

All frozen slotted dataclasses. ``payload`` and ``meta`` are canonical bytes;
``StoredRevision.payload`` is always the *logical* document — physical
encodings never escape the store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import UUID

from eventic.jsonx import JsonObject

Kind = Literal["create", "change"]
EncodingId = Literal["snapshot/1", "delta/1"]


@dataclass(frozen=True, slots=True)
class IntentRequest:
    """One durable delivery intent owed to a subscription."""

    subscription_id: str
    revision_id: UUID
    queue: str


@dataclass(frozen=True, slots=True)
class CommitRequest:
    """One appended revision, staged by the caller, committed by the store."""

    stream: str
    aggregate_id: UUID
    expected_revision: int | None
    kind: Kind
    schema_version: int
    payload: bytes
    digest: str
    meta: bytes
    meta_version: int
    fingerprint: str
    intents: tuple[IntentRequest, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class CommitResult:
    """The durable outcome of one request, from the database clock."""

    stream: str
    aggregate_id: UUID
    revision: int
    revision_id: UUID
    committed_at: datetime
    replayed: bool


@dataclass(frozen=True, slots=True)
class StoredRevision:
    """A revision as the store hands it up: logical document, decoded."""

    stream: str
    aggregate_id: UUID
    revision: int
    revision_id: UUID
    kind: str
    schema_version: int
    meta_version: int
    encoding: str
    payload: JsonObject
    digest: str
    meta: JsonObject
    committed_at: datetime


@dataclass(frozen=True, slots=True)
class ClaimedIntent:
    """A leased intent claimed by a worker, with enough to reconstruct the
    commit: the aggregate key is joined from the log row."""

    intent_id: UUID
    subscription_id: str
    revision_id: UUID
    queue: str
    attempts: int
    stream: str = ""
    aggregate_id: UUID | None = None
    revision: int = -1


@dataclass(frozen=True, slots=True)
class Settlement:
    """The outcome of one claimed delivery, applied in a short transaction."""

    intent_id: UUID
    status: Literal["delivered", "retry", "dead"]
    available_at: datetime | None = None
    error: str | None = None
