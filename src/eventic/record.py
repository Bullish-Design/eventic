"""``Record`` — a plain Pydantic v2 model that becomes a versioned aggregate.

Pure construction (I3); explicit ``save/update/edit/commit`` writes (I2);
deterministic ``version_id`` for every version including v0 (I4); reads via
``get/history/where``. No metaclass, no singleton, no implicit I/O.
"""
