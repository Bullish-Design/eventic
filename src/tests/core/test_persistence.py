"""Persistence + explicit-write tests (Step 3: I1, I2, I4, I5).

Includes the ported probe_02 scenario asserting the *loud* behaviour — the
rewrite's central regression target (R-C1/R-M3).
"""

import uuid

import pytest
from sqlalchemy import text

from eventic.connect import _reset, connect, engine
from eventic.errors import StaleVersionError
from eventic.plugins.persistence import SingleTableJSONB
from eventic.record import Record, _uuid5


class Doc(Record):
    title: str | None = None
    body: str | None = None


@pytest.fixture()
def db(tmp_path):
    _reset()
    connect(f"sqlite:///{tmp_path / 'e.db'}")
    yield
    _reset()


def _count() -> int:
    with engine().connect() as c:
        return c.execute(text("SELECT COUNT(*) FROM records")).scalar()


# ---------------------------------------------------------------------- #
# save / get / update / history / where
# ---------------------------------------------------------------------- #
def test_save_get_roundtrip(db):
    d = Doc(title="hello", body="world").save()
    got = Doc.get(d.id)
    assert got.title == "hello" and got.body == "world"
    assert got.version == 0
    assert got.version_id == d.version_id


def test_get_missing_raises_keyerror(db):
    with pytest.raises(KeyError):
        Doc.get(uuid.uuid4())


def test_update_returns_new_version_and_leaves_original(db):
    """R-C3: update() returns the new object; the original is untouched."""
    d = Doc(title="a").save()
    d2 = d.update(title="b")
    assert d2.version == 1 and d2.title == "b"
    assert d.version == 0 and d.title == "a"
    assert Doc.get(d.id).title == "b"


def test_commit_persists_current_state_as_next_version(db):
    d = Doc(title="a").save()
    d2 = d.update(title="b")
    d3 = d2.commit()
    assert d3.version == 2 and d3.title == "b"
    assert Doc.get(d.id).version == 2


def test_history_ordered_oldest_to_newest(db):
    d = Doc(title="a").save()
    d = d.update(title="b").update(title="c")
    hist = Doc.history(d.id)
    assert [h.version for h in hist] == [0, 1, 2]
    assert [h.title for h in hist] == ["a", "b", "c"]


def test_get_exact_version(db):
    d = Doc(title="a").save().update(title="b")
    assert Doc.get(d.id, version=0).title == "a"
    assert Doc.get(d.id, version=1).title == "b"
    with pytest.raises(KeyError):
        Doc.get(d.id, version=5)


def test_where_by_field_and_dotted_meta(db):
    a = Doc(title="x", meta={"status": "published"}).save()
    b = Doc(title="y", meta={"status": "draft"}).save()
    c = Doc(title="z", meta={"status": "draft"}).save()
    assert {r.id for r in Doc.where(title="x")} == {a.id}
    assert {r.id for r in Doc.where(**{"meta.status": "draft"})} == {b.id, c.id}
    assert Doc.where(**{"meta.status": "deleted"}) == []


def test_extra_fields_roundtrip(db):
    d = Doc(title="a", custom="zzz").save()
    assert Doc.get(d.id).custom == "zzz"
    d2 = d.update(custom="yyy")
    assert Doc.get(d.id).custom == "yyy"
    assert d2.version == 1


# ---------------------------------------------------------------------- #
# I5 — loud conflicts (the probe_02 regression)
# ---------------------------------------------------------------------- #
def test_two_writers_raise_stale_version(db):
    """probe_02's scenario now RAISES instead of silently losing data."""
    base = Doc(title=None, body=None).save()
    a = Doc.get(base.id)
    b = Doc.get(base.id)

    a.update(body="IMPORTANT A DATA")       # A writes v1
    with pytest.raises(StaleVersionError):
        b.update(title="IMPORTANT B DATA")  # B collides on (id, 1) -> LOUD

    fresh = Doc.get(base.id)
    assert fresh.body == "IMPORTANT A DATA"      # nothing silently lost
    assert fresh.title is None
    assert b.version == 0                        # B's object did not advance


def test_idempotent_replay_is_silent_noop(db):
    """Same (id, version, data) twice -> one row, no error (I5)."""
    d = Doc(title="x").save()
    d.save()  # byte-identical replay of v0
    assert _count() == 1


def test_append_level_identical_replay(db):
    p = SingleTableJSONB()
    rid = uuid.uuid4()
    row = {
        "version_id": _uuid5(rid, 0),
        "id": rid,
        "version": 0,
        "class_type": "Doc",
        "data": {"title": "same", "version": 0},
    }
    p.append(dict(row))
    p.append(dict(row))
    assert _count() == 1


def test_append_level_different_writer_raises(db):
    p = SingleTableJSONB()
    rid = uuid.uuid4()
    p.append(
        {"version_id": _uuid5(rid, 0), "id": rid, "version": 0, "class_type": "Doc",
         "data": {"title": "A", "version": 0}}
    )
    with pytest.raises(StaleVersionError):
        p.append(
            {"version_id": _uuid5(rid, 0), "id": rid, "version": 0, "class_type": "Doc",
             "data": {"title": "B", "version": 0}}  # same (id, ver), different bytes
        )
    assert _count() == 1
