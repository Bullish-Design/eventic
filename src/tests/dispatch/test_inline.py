"""Inline dispatch (Step 9, I7): handlers fire strictly post-commit; failures
are isolated; ordering is preserved; subscriptions are per-class (F10).

Record classes are defined inside each test: subscriptions are declarations
that live on the class, so a shared module-level class would accumulate them
across tests — exactly the "declarations persist" design, used honestly.
"""

from eventic import Record, on_commit


def test_handler_fires_post_commit(store):
    class Doc(Record, stream="dl_fires"):
        title: str | None = None

    seen = []

    @on_commit(Doc)
    def h_fires(ev):
        seen.append(ev.record.id)
        Doc.get(ev.record.id)  # must not raise -> post-commit timing

    d = Doc(title="x").save()
    assert seen == [d.id]


def test_update_handler_receives_delta(store):
    class Doc(Record, stream="dl_delta"):
        title: str | None = None

    events = []

    @on_commit(Doc, kind="update")
    def h_delta(ev):
        events.append((ev.record.id, ev.delta))

    d = Doc(title="a").save()
    d2 = d.update(title="b")
    assert d2.version == 1
    assert events == [(d.id, {"title": "b"})]


def test_create_handler_not_fired_on_update(store):
    class Doc(Record, stream="dl_create_only"):
        title: str | None = None

    kinds = []

    @on_commit(Doc, kind="create")
    def h_create_only(ev):
        kinds.append(ev.kind)

    d = Doc(title="a").save()
    d.update(title="b")
    assert kinds == ["create"]


def test_failing_handler_isolated(store):
    class Doc(Record, stream="dl_isolated"):
        title: str | None = None

    calls = []

    @on_commit(Doc)
    def h_bad(ev):
        raise RuntimeError("boom")

    @on_commit(Doc)
    def h_good(ev):
        calls.append(ev.record.id)

    d = Doc(title="x").save()  # must not propagate the failure
    assert calls == [d.id]
    assert Doc.get(d.id).title == "x"


def test_registration_order_preserved(store):
    class Doc(Record, stream="dl_order"):
        title: str | None = None

    order = []

    @on_commit(Doc)
    def h_first(ev):
        order.append("first")

    @on_commit(Doc)
    def h_second(ev):
        order.append("second")

    Doc(title="x").save()
    assert order == ["first", "second"]


def test_keyed_by_class_object(store):
    """A Note handler never fires for Doc — by class, not name."""
    class Doc(Record, stream="dl_doc_class"):
        title: str | None = None

    class Note(Record, stream="dl_note_class"):
        title: str | None = None

    note_calls = []

    @on_commit(Note)
    def h_note(ev):
        note_calls.append(ev.record.id)

    Doc(title="x").save()
    assert note_calls == []
    Note(title="y").save()
    assert len(note_calls) == 1


def test_mro_base_handler_fires_for_subclass(store):
    class Base(Record, stream="dl_base_class"):
        title: str | None = None

    class Sub(Base, stream="dl_sub_class"):
        pass

    names = []

    @on_commit(Base)
    def h_base(ev):
        names.append(type(ev.record).__name__)

    Sub(title="x").save()
    assert names == ["Sub"]


def test_draft_emits_one_update_event(store):
    class Doc(Record, stream="dl_draft"):
        title: str | None = None
        body: str | None = None

    events = []

    @on_commit(Doc, kind="update")
    def h_draft(ev):
        events.append(ev.delta)

    d = Doc(title="a").save()
    dr = d.draft()
    dr.title = "b"
    dr.body = "c"
    dr.commit()
    assert len(events) == 1  # exactly one event for the batch (I7)
    assert events[0] == {"title": "b", "body": "c"}


def test_interceptor_after_commit_receives_event(store):
    """F11 symmetric: after_commit gets the Event, like handlers."""
    from eventic import Interceptor

    class Doc(Record, stream="dl_watched"):
        title: str | None = None

    seen = []

    class Audit(Interceptor):
        def after_commit(self, event):
            seen.append((event.kind, event.record.title))

    class Watched(Record, stream="dl_watched2", interceptors=(Audit(),)):
        title: str | None = None

    Watched(title="x").save()
    assert seen == [("create", "x")]
