"""DBOS driver — opt-in. Importing this package imports nothing; the driver
module imports ``dbos`` explicitly and is never pulled in by the core (I6).
"""

from .dbos import DbosDispatcher, DbosStore, queue  # noqa: F401
