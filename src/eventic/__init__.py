"""
Public surface for Eventic.
Importing this module does **not** touch Postgres or DBOS; call
`eventic.init_eventic(engine)` during application start-up.
"""

# from .bootstrap import init_eventic
from .core.record import Record
from .core.properties import PropertiesBase
from .runtime import Eventic
from .events import on

__all__ = ["Eventic", "Record", "PropertiesBase", "on"]
