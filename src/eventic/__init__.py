"""eventic — versioned Pydantic aggregates whose history is an event stream.

Pure-Python core (pydantic + SQLAlchemy only, I6); durable async (DBOS), diff
storage, and typed columns are opt-in plugins (``eventic[dbos]`` / the plugin
seams). Importing this package never imports ``dbos`` or ``fastapi`` — the
optional adapter (``from eventic.dbos import ...``) is always an explicit
import (D17: a "conditional import if installed" would put dbos in
``sys.modules`` for everyone who has it installed, breaking the I6 contract).
"""

from .connect import connect
from .errors import (
    EventicError,
    MissingCapability,
    NotConnected,
    PluginConflictError,
    StaleVersionError,
)
from .events import on_commit
from .plugins import Plugin, Seam, use
from .plugins.codec import DiffStorage
from .record import Record

__version__ = "0.2.0"

__all__ = [
    "Record",
    "connect",
    "on_commit",
    "use",
    "DiffStorage",
    "Plugin",
    "Seam",
    "StaleVersionError",
    "PluginConflictError",
    "MissingCapability",
    "NotConnected",
    "EventicError",
]
