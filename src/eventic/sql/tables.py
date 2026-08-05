"""The physical schema — one source of truth.

SQLAlchemy Core ``Table`` objects, every check constraint, every index, and
``.with_variant(...)`` on every dialect-varying type. Alembic revisions are
generated from this metadata; ``alembic check`` is the gate that keeps them in
step.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Uuid as SqlUuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON

metadata = MetaData()

# JSONB on Postgres, plain JSON (TEXT) on SQLite. Declared once, varied by
# dialect, so a migration cannot forget the variant.
json_type: JSON = JSON().with_variant(JSONB(), "postgresql")

ENCODINGS_CONSTRAINT = "encoding IN ('snapshot/1','delta/1')"


eventic_revision = Table(
    "eventic_revision",
    metadata,
    Column[Any]("revision_id", SqlUuid, primary_key=True),
    Column[Any]("stream", Text, nullable=False),
    Column[Any]("aggregate_id", SqlUuid, nullable=False),
    Column[Any]("revision", Integer, nullable=False),
    Column[Any]("kind", Text, nullable=False),
    Column[Any]("schema_version", Integer, nullable=False),
    Column[Any]("meta_version", Integer, nullable=False),
    Column[Any]("encoding", Text, nullable=False),
    Column[Any]("payload", json_type, nullable=False),
    Column[Any]("digest", String(64), nullable=False),
    Column[Any]("meta", json_type, nullable=False),
    Column[Any]("committed_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("revision >= 0", name="ck_revision_nonneg"),
    CheckConstraint("kind IN ('create','change')", name="ck_kind"),
    CheckConstraint("schema_version >= 1", name="ck_schema_version"),
    CheckConstraint(ENCODINGS_CONSTRAINT, name="ck_encoding"),
    CheckConstraint("(revision = 0) = (kind = 'create')", name="ck_create_at_zero"),
    CheckConstraint("stream <> ''", name="ck_stream_nonempty"),
    UniqueConstraint("stream", "aggregate_id", "revision", name="uq_revision"),
    Index("ix_revision_sai", "stream", "aggregate_id", "revision"),
)

eventic_head = Table(
    "eventic_head",
    metadata,
    Column[Any]("stream", Text, primary_key=True),
    Column[Any]("aggregate_id", SqlUuid, primary_key=True),
    Column[Any]("revision", Integer, nullable=False),
    Column[Any]("revision_id", SqlUuid, nullable=False),
    Column[Any]("schema_version", Integer, nullable=False),
    Column[Any]("meta_version", Integer, nullable=False),
    Column[Any]("state", json_type, nullable=False),
    Column[Any]("digest", String(64), nullable=False),
    Column[Any]("meta", json_type, nullable=False),
    Column[Any]("committed_at", DateTime(timezone=True), nullable=False),
)

eventic_intent = Table(
    "eventic_intent",
    metadata,
    Column[Any]("intent_id", SqlUuid, primary_key=True),
    Column[Any]("subscription_id", Text, nullable=False),
    Column[Any]("revision_id", SqlUuid, nullable=False),
    Column[Any]("queue", Text, nullable=False),
    Column[Any]("status", Text, nullable=False),
    Column[Any]("attempts", Integer, nullable=False, server_default="0"),
    Column[Any]("available_at", DateTime(timezone=True), nullable=False),
    Column[Any]("leased_until", DateTime(timezone=True), nullable=True),
    Column[Any]("last_error", Text, nullable=True),
    Column[Any]("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("queue <> ''", name="ck_intent_queue"),
    CheckConstraint("status IN ('pending','leased','dead')", name="ck_intent_status"),
    UniqueConstraint("subscription_id", "revision_id", name="uq_intent_sub_rev"),
    Index("ix_intent_drain", "queue", "status", "available_at"),
)

eventic_schema = Table(
    "eventic_schema",
    metadata,
    Column[Any]("stream", Text, primary_key=True),
    Column[Any]("schema_version", Integer, primary_key=True),
    Column[Any]("fingerprint", String(64), nullable=False),
    Column[Any]("first_seen", DateTime(timezone=True), nullable=False),
)
