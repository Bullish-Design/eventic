"""The ``records`` table — the append-only version log (used by the default
persistence plugin). One immutable row per version; ``(id, version)`` unique;
``version_id`` is the deterministic primary key (I4).

Shape is unchanged from 0.1 minus the now-redundant ``properties`` column —
``meta`` lives inside ``data`` (folded by the Step-10 migration).
"""

import datetime as dt

from sqlalchemy import Column, DateTime, Index, Integer, String, Uuid, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import declarative_base
from sqlalchemy.types import JSON

Base = declarative_base()

# Generic JSON that compiles to JSONB on Postgres, plain JSON elsewhere.
JSONB = JSON().with_variant(postgresql.JSONB(), "postgresql")


def now_utc() -> dt.datetime:
    """Compact timezone-aware timestamp for ``created_ts``."""
    return dt.datetime.now(tz=dt.timezone.utc)


class RecordRow(Base):
    """Single table that stores **all** record versions (append-only, I1)."""

    __tablename__ = "records"
    __table_args__ = (
        UniqueConstraint("id", "version", name="uq_records_id_version"),  # I5: the loud-conflict pair
        Index("ix_records_id_ver", "id", "version"),                      # history/at-version reads
    )

    version_id = Column(Uuid(as_uuid=True), primary_key=True)  # deterministic uuid5 (I4)
    id = Column(Uuid(as_uuid=True), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    class_type = Column(String, nullable=False)
    created_ts = Column(DateTime(timezone=True), nullable=False, default=now_utc)
    data = Column(JSONB, nullable=False)  # the encoded version state (codec seam)
