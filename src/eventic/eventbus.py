"""Event core: ``Event`` + the ``on_commit`` handler registry (I7).

Handlers fire *after* the version row is durable, exactly once per commit,
keyed by the class object. The default ``sync`` delivery backend lives in
``plugins/delivery.py``; a durable backend is opt-in (``eventic[dbos]``).

NOTE (deviation D1): the old 0.1 ``events.py`` (``on.create``/``emit_create``)
keeps its module path until the Phase-6 swap; this module carries the *new*
event core under the working name ``eventbus.py`` and is renamed to
``events.py`` at Step 12.
"""
