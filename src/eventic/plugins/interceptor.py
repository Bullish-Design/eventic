"""Interceptor seam — stacking cross-cutting hooks on the write/read pipeline.

Hooks are inert by default (PLUGINS §3: a plugin implements only the methods
for the seams it occupies). Ordering (PLUGINS §5): ``before_commit`` runs
outer→inner and may **veto** (a failure aborts the write — nothing is half
written); ``after_commit`` / ``after_hydrate`` run inner→outer and a failure is
logged and isolated (like event handlers).
"""

from __future__ import annotations

from . import Plugin, Seam


class Veto(Exception):
    """Raise from ``before_commit`` to abort a write (no version is created)."""


class Interceptor(Plugin):
    """Stacking seam — 0..N providers, deterministic order by priority."""

    seam = Seam.INTERCEPTOR
    provides = set()
    requires = set()

    def before_commit(self, record):
        """Inspect/enrich the pending version, or raise ``Veto`` to abort."""
        return record

    def after_commit(self, record) -> None:
        """Runs only once the row is durable (audit, metrics)."""

    def after_hydrate(self, obj):
        """Transform a freshly reconstructed object (decrypt, redact)."""
        return obj

    def contribute_schema(self):
        """Optional extra columns/indexes for the persistence schema."""
        return None
