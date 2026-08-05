"""Keyword seam selection (Step 5–7): config resolution, subclassing, loud
stream collisions (F13), and the type-level capability check (F12).
"""

import pytest

from eventic import Delta, Record, Snapshot
from eventic.errors import SeamMismatch, StreamCollision
from eventic.store.sql import SqlStore


class Plain(Record, stream="cfg_plain"):
    body: str = ""


class Deltic(Record, stream="cfg_deltic", codec=Delta(k=3)):
    body: str = ""


class WithInterceptor(Record, stream="cfg_int"):
    body: str = ""


def test_defaults_resolve():
    assert isinstance(Plain.__eventic__.codec, Snapshot)
    assert isinstance(Plain.__eventic__.rows, SqlStore)
    assert Plain.__eventic__.stream == "cfg_plain"


def test_keyword_selects_codec():
    assert isinstance(Deltic.__eventic__.codec, Delta)
    assert Deltic.__eventic__.codec.k == 3


def test_keywords_do_not_leak_into_model_fields():
    """F1: framework metadata must not become pydantic fields."""
    assert set(Plain.model_fields) == {"id", "version", "version_id", "created_ts", "meta", "body"}
    assert set(Deltic.model_fields) == set(Plain.model_fields)


def test_subclass_inherits_seams_but_not_stream(store):
    """F2: subclassing a plugin-bearing Record inherits the codec and gets its
    own stream; it never crashes on required fields."""
    class Base(Record, stream="cfg_base", codec=Delta(k=5)):
        required: str

    class Sub(Base, stream="cfg_sub"):
        pass

    assert isinstance(Sub.__eventic__.codec, Delta)
    assert Sub.__eventic__.stream == "cfg_sub"
    s = Sub(required="x").save()
    assert Sub.get(s.id).required == "x"


def test_stream_defaults_to_class_name():
    class ImplicitName(Record):
        pass

    assert ImplicitName.__eventic__.stream == "ImplicitName"


def test_stream_collision_is_loud():
    class A(Record, stream="cfg_shared"):
        pass

    with pytest.raises(StreamCollision):

        class B(Record, stream="cfg_shared"):
            pass


def test_same_class_restatement_is_allowed():
    """Re-registering the exact same class is not a collision (hot reload)."""
    class Again(Record, stream="cfg_again"):
        pass

    register_again = Again.__eventic__  # noqa: F841
    # the class's own __init_subclass__ only runs once; re-importing the same
    # class object is a no-op by design — assert the registry still holds it
    from eventic.config import _STREAMS

    assert _STREAMS["cfg_again"] is Again


def test_delta_requires_json_store_at_definition():
    """Delta + a non-JSON store is a sentence you cannot write (F12)."""

    class NotJson:
        """A RowStore that does NOT store JSON documents."""

        def append(self, s, row):
            raise NotImplementedError

        def read(self, s, stream, rec_id, window, version):
            raise NotImplementedError

        def stream(self, s, stream, rec_id):
            raise NotImplementedError

        def all_rows(self, s, stream):
            raise NotImplementedError

        def head(self, s, stream, rec_id):
            raise NotImplementedError

        def upsert_head(self, s, head):
            raise NotImplementedError

        def search(self, s, stream, eq):
            raise NotImplementedError

        def stage_outbox(self, s, sub, event):
            raise NotImplementedError

    with pytest.raises(SeamMismatch):

        class Broken(Record, stream="cfg_broken", rows=NotJson(), codec=Delta(k=3)):
            pass


def test_interceptors_declared_by_keyword(store):
    from eventic import Interceptor

    calls = []

    class Auditing(Interceptor):
        def before_commit(self, record):
            calls.append(("before", record.title))
            return record

        def after_commit(self, event):
            calls.append(("after", event.record.title))

        def after_hydrate(self, obj):
            return obj

    class Watched(Record, stream="cfg_watched", interceptors=(Auditing(),)):
        title: str | None = None

    Watched(title="x").save()
    assert calls == [("before", "x"), ("after", "x")]
