"""Eventic error hierarchy.

All library-raised exceptions derive from :class:`EventicError` so callers can
catch one base type. The names below are the *contract*: ``StaleVersionError``
(I5 loud conflicts), ``PluginConflictError`` (two providers on one exclusive
seam, raised at class definition), ``MissingCapability`` (an unmet ``requires``
token), and ``NotConnected`` (using the store before ``connect()``).
"""


class EventicError(Exception):
    """Base class for every error raised by eventic."""


class NotConnected(EventicError):
    """Raised when the engine registry is empty — call ``connect(url)`` first."""


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


class PluginConflictError(EventicError):
    """Two providers attached to one *exclusive* seam — at class definition."""


class MissingCapability(EventicError):
    """A plugin's ``requires`` tokens are not satisfied by the assembled set."""
