"""
Single-table schema: every version of every Record lives here.
"""

import uuid
import datetime as dt

from sqlalchemy import Column, DateTime, Integer, String, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import declarative_base

Base = declarative_base()


def now_utc() -> dt.datetime:  # compact timezone‑aware timestamp
    return dt.datetime.now(tz=dt.timezone.utc)


class RecordRow(Base):
    """Single table that stores **all** record versions."""

    __tablename__ = "records"

    version_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    id = Column(UUID(as_uuid=True), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    class_type = Column(String, nullable=False)
    created_ts = Column(DateTime(timezone=True), default=now_utc, nullable=False)
    properties = Column(JSONB, nullable=True)
    data = Column(JSONB, nullable=False)
