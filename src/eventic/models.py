"""The ``records`` table — the append-only version log (used by the default
persistence plugin). One immutable row per version; ``(id, version)`` unique;
``version_id`` is the deterministic primary key (I4).
"""
