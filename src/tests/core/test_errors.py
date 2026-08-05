"""The error hierarchy (Step 21): everything derives from EventicError;
RecordNotFound is also a KeyError (F15); Veto is exported (F12).
"""

import uuid

from eventic import (
    ConfigError,
    EventicError,
    HandlerCollision,
    Interceptor,
    NotConnected,
    RecordNotFound,
    SeamMismatch,
    StaleVersionError,
    StreamCollision,
    UsageError,
    Veto,
)


def test_everything_derives_from_eventic_error():
    for exc in (
        NotConnected,
        RecordNotFound,
        StaleVersionError,
        StreamCollision,
        HandlerCollision,
        SeamMismatch,
        ConfigError,
        UsageError,
        Veto,
    ):
        assert issubclass(exc, EventicError), exc


def test_record_not_found_satisfies_both_contracts():
    assert issubclass(RecordNotFound, KeyError)  # except KeyError still works
    err = RecordNotFound("Doc", uuid.uuid4(), version=3)
    assert "v3" in str(err)


def test_interceptor_base_is_a_plain_class():
    """Not a pydantic model, not a Plugin — just hooks."""
    itc = Interceptor()
    r = object()
    assert itc.before_commit(r) is r
    assert itc.after_commit(object()) is None
    assert itc.after_hydrate(r) is r
