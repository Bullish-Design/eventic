"""
Single entry-point that wires SQLAlchemy + DBOS into Eventic.
Call once, e.g. in FastAPI startup or Django AppConfig.ready().
"""

import os
from typing import TYPE_CHECKING

from sqlalchemy.engine import Engine

from .core.record import Record
from .persistence.store import RecordStore
from .persistence.models import Base

if TYPE_CHECKING:  # for type-checkers
    from .core.record import _T_Record


def init_eventic(engine: Engine) -> None:
    """
    Initialise the global RecordStore and inject it into **all** current
    Record subclasses.

    Table creation is a development convenience: by default the schema is
    created if missing (idempotent), but Alembic migrations
    (``alembic upgrade head``) are the source of truth in production — set
    ``EVENTIC_AUTO_CREATE_TABLES=0`` to disable the auto-create entirely.
    """
    if os.environ.get("EVENTIC_AUTO_CREATE_TABLES", "1") not in {"0", "false", "False"}:
        Base.metadata.create_all(engine)
    global_store = RecordStore(engine)

    # Attach store to Record *and* every existing subclass
    Record._store = global_store  # type: ignore[attr-defined]
    for subclass in Record.__subclasses__():
        subclass._store = global_store  # type: ignore[attr-defined]
