"""``connect(url)`` — the one-engine process registry.

Replaces ``Eventic.init``/``init_eventic`` for the DBOS-free core (I6). The
registry is a single module-level engine; the default persistence plugin and
every read/write go through ``engine()``.
"""
