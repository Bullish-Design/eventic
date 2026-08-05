"""Interceptor base + ``Veto``.

Ordering (CONCEPT §4/§5): ``before_commit`` runs outer→inner and may **veto**
(a failure aborts the write — nothing is half written); ``after_commit`` /
``after_hydrate`` run inner→outer and a failure is logged and isolated (like
event handlers). ``before_commit``'s return value **is threaded** (F11) —
it is a genuine transformer, symmetric with ``after_hydrate``.
"""

from __future__ import annotations

from .errors import Veto  # noqa: F401  (re-exported: F12)

__all__ = ["Interceptor", "Veto"]


class Interceptor:
    """Stacking seam — 0..N interceptors, in declaration order."""

    def before_commit(self, record):
        """Inspect/enrich the pending version, or raise ``Veto`` to abort."""
        return record

    def after_commit(self, event) -> None:
        """Runs only once the version is durable (audit, metrics)."""

    def after_hydrate(self, obj):
        """Transform a freshly reconstructed object (decrypt, redact)."""
        return obj
