"""Pure-construction & deterministic-identity tests (Step 2: I3, I4)."""

import uuid

from sqlalchemy import text

from eventic.connect import _reset, connect, engine
from eventic.record import Record, _uuid5


def _reset_engine():
    _reset()


class Todo(Record):
    text: str
    done: bool = False


def test_construction_writes_nothing(tmp_path):
    """I3/R-E1: building a model must not touch the database — even connected."""
    _reset_engine()
    connect(f"sqlite:///{tmp_path / 'a.db'}")
    before = engine().connect().execute(text("SELECT COUNT(*) FROM records")).scalar()
    Todo(text="hi")
    Todo(text="bye", done=True)
    after = engine().connect().execute(text("SELECT COUNT(*) FROM records")).scalar()
    assert before == after == 0


def test_construction_is_in_memory_only(tmp_path):
    """The object is fully usable without any engine existing at all."""
    _reset_engine()
    t = Todo(text="offline")
    assert t.text == "offline" and t.version == 0


def test_v0_version_id_is_deterministic():
    """I4: v0 gets the same uuid5 identity as every other version (R-C2)."""
    t = Todo(text="x")
    expected = uuid.uuid5(uuid.NAMESPACE_URL, f"eventic:{t.id}:0")
    assert t.version_id == expected


def test_same_id_same_version_same_version_id():
    t1 = Todo(text="a")
    t2 = Todo(text="b")  # different fields, same id+version
    # force identical id to prove identity derives only from (id, version)
    fixed = uuid.uuid4()
    a = Todo(text="a", id=fixed)
    b = Todo(text="b", id=fixed)
    assert a.version_id == b.version_id == _uuid5(fixed, 0)
    assert t1.version_id != t2.version_id  # different ids → different version_ids


def test_managed_fields_present():
    t = Todo(text="x")
    assert t.version == 0
    assert t.created_ts is None
    assert t.meta == {}
