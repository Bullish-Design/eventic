"""Post-commit event tests (Step 5: I7, R-C4, H6)."""

import pytest

from eventic.connect import _reset, connect
from eventic.events import _reset_handlers, on_commit
from eventic.record import Record


class Doc(Record):
    title: str | None = None


class Note(Record):
    title: str | None = None


@pytest.fixture(autouse=True)
def clean(tmp_path):
    _reset()
    _reset_handlers()
    connect(f"sqlite:///{tmp_path / 'e.db'}")
    yield
    _reset()
    _reset_handlers()


def test_handler_fires_post_commit_and_sees_row():
    """H6/I7: the row is durable before any handler runs."""
    seen = []

    @on_commit(Doc)
    def h(ev):
        seen.append(ev.record.id)
        Doc.get(ev.record.id)  # must not raise -> post-commit timing

    d = Doc(title="x").save()
    assert seen == [d.id]


def test_update_handler_receives_delta():
    events = []

    @on_commit(Doc, kind="update")
    def h(ev):
        events.append((ev.record.id, ev.delta))

    d = Doc(title="a").save()
    d2 = d.update(title="b")
    assert d2.version == 1
    assert events == [(d.id, {"title": "b"})]


def test_create_handler_not_fired_on_update():
    kinds = []

    @on_commit(Doc, kind="create")
    def h(ev):
        kinds.append(ev.kind)

    d = Doc(title="a").save()
    d.update(title="b")
    assert kinds == ["create"]


def test_failing_handler_isolated_and_others_still_run():
    calls = []

    @on_commit(Doc)
    def bad(ev):
        raise RuntimeError("boom")

    @on_commit(Doc)
    def good(ev):
        calls.append(ev.record.id)

    d = Doc(title="x").save()  # must not propagate the failure
    assert calls == [d.id]
    assert Doc.get(d.id).title == "x"


def test_registration_order_preserved():
    order = []

    @on_commit(Doc)
    def first(ev):
        order.append("first")

    @on_commit(Doc)
    def second(ev):
        order.append("second")

    Doc(title="x").save()
    assert order == ["first", "second"]


def test_keyed_by_class_object():
    """A Note handler never fires for Doc — keyed by class object, not name."""
    note_calls = []

    @on_commit(Note)
    def h(ev):
        note_calls.append(ev.record.id)

    Doc(title="x").save()
    assert note_calls == []
    Note(title="y").save()
    assert len(note_calls) == 1


def test_mro_base_handler_fires_for_subclass():
    names = []

    @on_commit(Record)
    def h(ev):
        names.append(type(ev.record).__name__)

    Doc(title="x").save()
    assert names == ["Doc"]


def test_edit_emits_one_update_event():
    events = []

    @on_commit(Doc, kind="update")
    def h(ev):
        events.append(ev.delta)

    d = Doc(title="a").save()
    with d.edit() as e:
        e.title = "b"
        e.body = "c"
    assert len(events) == 1  # exactly one event for the batch (I7)
    assert events[0] == {"title": "b", "body": "c"}
