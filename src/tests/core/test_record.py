"""Record value semantics (Step 17/18): pure construction, frozen, explicit
writes, draft().commit() returning the new version (F6), created_ts (F5),
I5 replays.
"""

import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from eventic import Draft, Record
from eventic.errors import RecordNotFound, StaleVersionError, UsageError
from eventic.identity import version_id
from eventic.store.schema import LogRow


class Todo(Record, stream="rec_todo"):
    text: str = ""
    done: bool = False


class Doc(Record, stream="rec_doc"):
    title: str | None = None
    body: str | None = None


def _count(store):
    with Session(store.engine) as s:
        return len(s.execute(select(LogRow)).scalars().all())


# --------------------------------------------------------------------- #
# pure construction (I3) + identity (I4)
# --------------------------------------------------------------------- #
def test_construction_is_in_memory_only():
    """Fully usable with no Store at all — no I/O, no events."""
    t = Todo(text="offline")
    assert t.text == "offline" and t.version == 0
    assert t.version_id == version_id(t.id, 0)


def test_construction_writes_nothing(store):
    before = _count(store)
    Todo(text="hi")
    Todo(text="bye", done=True)
    assert _count(store) == before == 0


def test_v0_version_id_is_deterministic():
    t = Todo(text="x")
    assert t.version_id == uuid.uuid5(uuid.NAMESPACE_URL, f"eventic:{t.id}:0")


def test_managed_fields_present():
    t = Todo(text="x")
    assert t.version == 0
    assert t.created_ts is None
    assert t.meta == {}


# --------------------------------------------------------------------- #
# frozen + extra=forbid (F14)
# --------------------------------------------------------------------- #
def test_extra_fields_rejected():
    with pytest.raises(ValidationError):
        Todo(txt="typo")  # silently persisted forever in 0.2


def test_frozen_model_rejects_assignment():
    t = Todo(text="a")
    with pytest.raises(ValidationError):
        t.text = "b"


def test_managed_fields_cannot_be_client_set():
    """version_id is always derived, never honored from input (I4)."""
    rid = uuid.uuid4()
    t = Todo(text="x", id=rid, version=3, version_id=uuid.uuid4())
    assert t.version_id == version_id(rid, 3)


# --------------------------------------------------------------------- #
# writes
# --------------------------------------------------------------------- #
def test_save_get_roundtrip_and_created_ts(store):
    d = Doc(title="hello", body="world").save()
    got = Doc.get(d.id)
    assert got.title == "hello" and got.body == "world"
    assert got.version == 0
    assert got.version_id == d.version_id
    assert got.created_ts is not None  # F5


def test_get_missing_raises_record_not_found(store):
    with pytest.raises(KeyError):
        Doc.get(uuid.uuid4())


def test_get_missing_version_raises(store):
    d = Doc(title="a").save()
    with pytest.raises(RecordNotFound):
        Doc.get(d.id, version=5)


def test_update_returns_new_version_and_leaves_original(store):
    d = Doc(title="a").save()
    d2 = d.update(title="b")
    assert d2.version == 1 and d2.title == "b"
    assert d.version == 0 and d.title == "a"
    assert Doc.get(d.id).title == "b"


def test_save_only_for_v0(store):
    d = Doc(title="a").save().update(title="b")
    with pytest.raises(UsageError):
        d.save()


def test_exact_version_reads(store):
    d = Doc(title="a").save().update(title="b")
    assert Doc.get(d.id, version=0).title == "a"
    assert Doc.get(d.id, version=1).title == "b"


def test_idempotent_replay_is_silent_noop(store):
    d = Doc(title="x").save()
    d.save()  # byte-identical replay of v0
    assert _count(store) == 1


def test_stale_version_is_loud(store):
    base = Doc(title=None, body=None).save()
    a = Doc.get(base.id)
    b = Doc.get(base.id)
    a.update(body="IMPORTANT A DATA")
    with pytest.raises(StaleVersionError):
        b.update(title="IMPORTANT B DATA")
    fresh = Doc.get(base.id)
    assert fresh.body == "IMPORTANT A DATA"
    assert b.version == 0


# --------------------------------------------------------------------- #
# draft (F6)
# --------------------------------------------------------------------- #
def test_draft_returns_new_version(store):
    d = Doc(title="a").save()
    dr = d.draft()
    dr.title = "b"
    dr.body = "c"
    fresh = dr.commit()
    assert fresh.version == 1  # the RETURNED value is the new version
    assert Doc.get(d.id).title == "b" and Doc.get(d.id).body == "c"
    assert d.version == 0  # the original handle is untouched


def test_draft_nested_meta_mutation(store):
    d = Doc(title="a", meta={"status": "draft"}).save()
    dr = d.draft()
    dr.meta["status"] = "published"
    fresh = dr.commit()
    assert fresh.meta == {"status": "published"}
    assert _count(store) == 2  # exactly ONE new version


def test_draft_empty_commit_writes_nothing(store):
    d = Doc(title="a").save()
    assert d.draft().commit() is d  # no changes -> same object, no write
    assert _count(store) == 1


def test_draft_managed_fields_ignored(store):
    d = Doc(title="a").save()
    dr = d.draft()
    dr.version = 99  # managed — must not be written
    dr.title = "b"
    fresh = dr.commit()
    assert fresh.version == 1


def test_draft_is_a_scratch_copy(store):
    d = Doc(title="a", meta={"status": "draft"}).save()
    dr = d.draft()
    dr.title = "b"
    dr.meta["status"] = "published"
    assert d.title == "a" and d.version == 0
    assert d.meta == {"status": "draft"}


# --------------------------------------------------------------------- #
# history / where
# --------------------------------------------------------------------- #
def test_history_ordered_oldest_to_newest(store):
    d = Doc(title="a").save().update(title="b").update(title="c")
    hist = Doc.history(d.id)
    assert [h.version for h in hist] == [0, 1, 2]
    assert [h.title for h in hist] == ["a", "b", "c"]
    assert all(h.created_ts is not None for h in hist)


def test_where_by_field_and_dotted_meta(store):
    a = Doc(title="x", meta={"status": "published"}).save()
    b = Doc(title="y", meta={"status": "draft"}).save()
    c = Doc(title="z", meta={"status": "draft"}).save()
    assert {r.id for r in Doc.where(title="x")} == {a.id}
    assert {r.id for r in Doc.where(**{"meta.status": "draft"})} == {b.id, c.id}
    assert Doc.where(**{"meta.status": "deleted"}) == []


def test_draft_class_is_exported():
    assert isinstance(Doc(title="a").draft(), Draft)
