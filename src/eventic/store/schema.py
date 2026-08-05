"""The schema triad (CONCEPT §2.1).

``eventic_log``    — append-only, immutable — the truth (I1)
``eventic_head``   — one row per aggregate — derived; a cache with the same
                     transaction boundary
``eventic_outbox`` — pending durable deliveries — drained, then reaped

``head`` and ``outbox`` are **derived and rebuildable**; ``cli.py rebuild-heads``
proves the log is the truth by rebuilding the head from it.

Three deliberate choices:

- One composite index replaces v2's three overlapping ones (F18), and
  ``stream`` — which every query filters on and which had no index at all — is
  the leading column.
- ``snapshot`` is a column, not a JSON key, so a delta codec's window query is
  a single indexed range scan (F17).
- ``UNIQUE(version_id, handler_id)`` makes outbox staging idempotent under
  replay, mirroring I5 at the delivery layer.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Uuid,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import declarative_base
from sqlalchemy.types import JSON

Base = declarative_base()

# Generic JSON that compiles to JSONB on Postgres, plain JSON elsewhere.
JSONB = JSON().with_variant(postgresql.JSONB(), "postgresql")


def now_utc() -> dt.datetime:
    """Compact timezone-aware timestamp."""
    return dt.datetime.now(tz=dt.timezone.utc)


class LogRow(Base):
    """The truth: one immutable version per row (I1)."""

    __tablename__ = "eventic_log"
    __table_args__ = (
        UniqueConstraint("id", "version", name="uq_eventic_log_id_version"),  # I5
        Index("ix_eventic_log_stream_id_version", "stream", "id", "version"),
        Index(
            "ix_eventic_log_snapshot",
            "stream",
            "id",
            "version",
            postgresql_where=text("snapshot"),
            sqlite_where=text("snapshot"),
        ),
    )

    version_id = Column(Uuid(as_uuid=True), primary_key=True)  # uuid5 (I4)
    stream = Column(String, nullable=False)
    id = Column(Uuid(as_uuid=True), nullable=False)
    version = Column(Integer, nullable=False)
    kind = Column(String, nullable=False)  # 'create' | 'update'
    snapshot = Column(Boolean, nullable=False)  # codec-declared
    committed_at = Column(DateTime(timezone=True), nullable=False, default=now_utc)
    data = Column(JSONB, nullable=False)  # USER STATE ONLY (CONCEPT §5.1)


class HeadRow(Base):
    """Derived: one row per aggregate; a cache with the same txn boundary."""

    __tablename__ = "eventic_head"
    __table_args__ = (
        Index("ix_eventic_head_state", "state", postgresql_using="gin"),
    )

    stream = Column(String, primary_key=True)
    id = Column(Uuid(as_uuid=True), primary_key=True)
    version = Column(Integer, nullable=False)
    version_id = Column(Uuid(as_uuid=True), nullable=False)
    committed_at = Column(DateTime(timezone=True), nullable=False)
    state = Column(JSONB, nullable=False)  # fully decoded head state


class OutboxRow(Base):
    """Derived: pending durable deliveries."""

    __tablename__ = "eventic_outbox"
    __table_args__ = (
        UniqueConstraint("version_id", "handler_id", name="uq_eventic_outbox_once"),
        Index("ix_eventic_outbox_ready", "available_at"),
    )

    seq = Column(
        BigInteger().with_variant(Integer(), "sqlite"),  # SQLite needs INTEGER PK to autoincrement
        primary_key=True,
        autoincrement=True,
    )
    version_id = Column(Uuid(as_uuid=True), nullable=False)
    stream = Column(String, nullable=False)
    record_id = Column(Uuid(as_uuid=True), nullable=False)
    version = Column(Integer, nullable=False)
    kind = Column(String, nullable=False)
    delta = Column(JSONB, nullable=True)
    handler_id = Column(String, nullable=False)
    queue = Column(String, nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    available_at = Column(DateTime(timezone=True), nullable=False, default=now_utc)
