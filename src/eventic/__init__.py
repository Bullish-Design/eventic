"""eventic — a versioned document store with transactional change notification.

``import eventic`` imports pydantic and nothing else; ``eventic.sql`` is the
first module that imports SQLAlchemy.
"""

from __future__ import annotations

from eventic.app import App
from eventic.envelopes import Commit, Page, Revision
from eventic.meta import NoMeta
from eventic.stream import Stream
from eventic.subscription import Backoff, Inline, Outbox, Subscription

__version__ = "1.0.0"

__all__ = [
    "App",
    "Backoff",
    "Commit",
    "Inline",
    "NoMeta",
    "Outbox",
    "Page",
    "Revision",
    "Stream",
    "Subscription",
    "__version__",
]
