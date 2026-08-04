"""
Single-table schema: every version of every Record lives here.

Portable across SQLite (tests, local dev) and Postgres. The JSON columns use
a generic ``JSON`` type with a ``JSONB`` variant on Postgres so the same
model works on both engines; Postgres deployments still get JSONB storage
plus GIN-friendly behavior.
"""

import uuid
import datetime as dt

from sqlalchemy import Column, DateTime, Integer, String, Uuid, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import JSON
from sqlalchemy.orm import declarative_base

Base = declarative_base()

# Generic JSON that compiles to JSONB on Postgres, plain JSON elsewhere.
# The generic JSON comparator implements `contains` on SQLite too, so
# find_by_properties works on both dialects.
JSONB = JSON().with_variant(postgresql.JSONB(), "postgresql")


def now_utc() -> dt.datetime:  # compact timezone-aware timestamp
    return dt.datetime.now(tz=dt.timezone.utc)


class RecordRow(Base):
    """Single table that stores **all** record versions."""

    __tablename__ = "records"
    __table_args__ = (
        UniqueConstraint("id", "version", name="uq_records_id_version"),  # Step 4.1 (C6)
    )

    version_id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    id = Column(Uuid(as_uuid=True), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    class_type = Column(String, nullable=False)
    created_ts = Column(DateTime(timezone=True), default=now_utc, nullable=False)
    properties = Column(JSONB, nullable=False, default=dict)  # L5/L6: non-null
    data = Column(JSONB, nullable=False)
