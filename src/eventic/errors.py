"""Eventic error hierarchy.

Every library-raised exception derives from :class:`EventicError`, so callers
can catch one base type. ``RecordNotFound`` also subclasses ``KeyError`` so
``except KeyError`` keeps working (F15). ``Veto`` is exported from the package
root (F12).
"""


class EventicError(Exception):
    """Base class for every error raised by eventic."""


class NotConnected(EventicError):
    """No ``Store`` is active — call ``eventic.connect(url)`` or use a
    ``Store`` context manager first."""


class RecordNotFound(EventicError, KeyError):
    """An aggregate (or exact version) does not exist (F15)."""

    def __init__(self, cls_name: str, rec_id, version: int | None = None):
        suffix = f" v{version}" if version is not None else ""
        super().__init__(f"{cls_name} {rec_id}{suffix} not found")
        self.cls_name = cls_name
        self.rec_id = rec_id
        self.version = version


class StaleVersionError(EventicError):
    """Two different writers at the same ``(id, version)`` (I5).

    Only a byte-identical replay of the exact same ``version_id`` is silently
    idempotent; any *other* writer colliding on the ``(id, version)`` pair is a
    lost-update-in-waiting and must hear about it loudly.
    """

    def __init__(self, id, version):
        super().__init__(f"aggregate {id} already has a different version {version}")
        self.id = id
        self.version = version


class StreamCollision(EventicError):
    """Two classes claimed the same stream name (F13). One stream, one class."""


class HandlerCollision(EventicError):
    """Two functions registered under the same ``module:qualname`` (F22)."""


class SeamMismatch(EventicError):
    """A codec requires a store capability the chosen rows provider lacks
    (e.g. ``Delta`` requires a JSON-shaped store). Raised at class definition."""


class ConfigError(EventicError):
    """An invalid ``on_commit``/class configuration."""


class UsageError(EventicError):
    """A public API misused (e.g. ``save()`` on a version != 0)."""


class Veto(EventicError):
    """Raise from an interceptor's ``before_commit`` to abort a write."""
