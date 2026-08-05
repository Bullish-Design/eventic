"""edit() batch-write tests (Step 4: R-P1 write amplification)."""

import pytest
from sqlalchemy import text

from eventic.connect import _reset, connect, engine
from eventic.record import Record


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


def test_edit_batches_into_one_version(db):
    d = Doc(title="a").save()
    with d.edit() as e:
        e.title = "b"
        e.body = "c"
    assert _count() == 2  # v0 + exactly ONE edit version
    fresh = Doc.get(d.id)
    assert fresh.title == "b" and fresh.body == "c"
    assert fresh.version == 1


def test_edit_empty_writes_nothing(db):
    d = Doc(title="a").save()
    with d.edit():
        pass
    assert _count() == 1


def test_edit_identical_value_writes_nothing(db):
    d = Doc(title="a").save()
    with d.edit() as e:
        e.title = "a"  # same value -> no-op guard
    assert _count() == 1


def test_edit_nested_meta_change(db):
    d = Doc(title="a", meta={"status": "draft"}).save()
    with d.edit() as e:
        e.meta["status"] = "published"
    assert _count() == 2
    assert Doc.get(d.id).meta == {"status": "published"}


def test_edit_original_object_untouched(db):
    d = Doc(title="a", meta={"status": "draft"}).save()
    with d.edit() as e:
        e.title = "b"
        e.meta["status"] = "published"
    assert d.title == "a" and d.version == 0
    assert d.meta == {"status": "draft"}  # the draft was a copy (I3)


def test_edit_cannot_set_version(db):
    d = Doc(title="a").save()
    with d.edit() as e:
        e.version = 99  # managed field — must be ignored
    assert Doc.get(d.id).version == 1


def test_edit_exception_rolls_back_all_changes(db):
    d = Doc(title="a").save()
    with pytest.raises(RuntimeError):
        with d.edit() as e:
            e.title = "b"
            raise RuntimeError("abort")
    assert _count() == 1  # nothing written
    assert Doc.get(d.id).title == "a"
