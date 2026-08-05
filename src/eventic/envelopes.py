"""Envelopes: ``Revision``, ``Commit``, ``Page`` — frozen generic Pydantic models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

Kind = Literal["create", "change"]


class Revision[T: BaseModel, M: BaseModel](BaseModel):
    """One immutable committed state of an aggregate."""

    model_config = ConfigDict(frozen=True)

    stream: str
    id: UUID
    revision: int
    revision_id: UUID
    state: T
    meta: M
    committed_at: datetime
    digest: str


class Commit[T: BaseModel, M: BaseModel](BaseModel):
    """The change-feed envelope handed to subscription handlers."""

    model_config = ConfigDict(frozen=True)

    kind: Kind
    revision: Revision[T, M]
    changed: frozenset[str]


class Page[X](BaseModel):
    """A bounded page of results; ``cursor`` is opaque and ``None`` when exhausted."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    items: tuple[X, ...]
    cursor: str | None
