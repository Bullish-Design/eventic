"""
Single entry-point that wires SQLAlchemy + DBOS into Eventic.
Call once, e.g. in FastAPI startup or Django AppConfig.ready().
"""

from typing import TYPE_CHECKING

# from dbos import DBOS  # pip install dbos
from sqlalchemy.engine import Engine

from .core.record import Record
from .persistence.store import RecordStore
from .persistence.models import Base

if TYPE_CHECKING:  # for type-checkers
    from .core.record import _T_Record


def init_eventic(engine: Engine) -> None:
    """
    Initialise the global RecordStore, link DBOS to Postgres,
    and inject the store into **all** current Record subclasses.
    """
    Base.metadata.create_all(engine)  # ← this line creates table
    global_store = RecordStore(engine)
    # DBOS.link_to_db(engine)

    # Attach store to Record *and* every existing subclass
    Record._store = global_store  # type: ignore[attr-defined]
    for subclass in Record.__subclasses__():
        subclass._store = global_store  # type: ignore[attr-defined]
