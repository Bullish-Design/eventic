"""UnitOfWork — the durability line (Step 2, F3).

Events flush only after COMMIT; rollback discards them; a nested unit of work
stages into the parent and emits once with the outermost; a foreign session's
commit flushes via the after_commit listener.
"""

import pytest
from sqlalchemy.orm import Session

from eventic.event import Event
from eventic.store.unit_of_work import UnitOfWork


def test_events_flush_only_after_commit(store):
    from eventic import Record, on_commit

    class Doc(Record, stream="uow_doc"):
        n: int = 0

    seen = []

    @on_commit(Doc, kind="create")
    def h(ev):
        seen.append(ev.record.id)

    r = Doc(n=1)
    with store.unit_of_work() as uow:
        uow.stage(Event(kind="create", record=r))
        assert seen == []  # NOT durable yet — nothing flushed
    assert seen == [r.id]  # flushed after COMMIT


def test_exception_inside_block_rolls_back_and_emits_nothing(store):
    from eventic import Record, on_commit

    class Doc(Record, stream="uow_exc"):
        n: int = 0

    seen = []

    @on_commit(Doc, kind="create")
    def h(ev):
        seen.append(ev.record.id)

    r = Doc(n=1)
    with pytest.raises(RuntimeError):
        with store.unit_of_work() as uow:
            uow.stage(Event(kind="create", record=r))
            raise RuntimeError("abort")
    assert seen == []


def test_save_inside_outer_uow_emits_once_with_outer(store):
    """Nesting: inner saves stage into the parent; one commit, all events."""
    from eventic import Record, on_commit

    class Doc(Record, stream="uow_nest"):
        n: int = 0

    seen = []

    @on_commit(Doc, kind="*")
    def h(ev):
        seen.append(ev.record.version)

    with store.unit_of_work():
        r = Doc(n=1).save()
        r2 = r.update(n=2)
        assert seen == []  # outer hasn't committed yet
    assert seen == [0, 1]  # exactly one event per write, at the outer commit
    assert Doc.get(r2.id).version == 1


def test_foreign_session_commit_flushes_via_listener(store):
    from eventic import Record, on_commit

    class Doc(Record, stream="uow_foreign"):
        n: int = 0

    seen = []

    @on_commit(Doc, kind="create")
    def h(ev):
        seen.append(ev.record.id)

    r = Doc(n=1)
    s = Session(store.engine, future=True)
    uow = UnitOfWork(s, owns_commit=False)  # the OWNER commits
    with uow:
        uow.stage(Event(kind="create", record=r))
        assert seen == []  # not durable yet
    s.commit()  # the owner's commit is the durability line
    assert seen == [r.id]
    s.close()


def test_foreign_session_rollback_discards(store):
    """F3's core: a version that never becomes durable must never fire."""
    from eventic import Record, on_commit

    class Doc(Record, stream="uow_rb"):
        n: int = 0

    seen = []

    @on_commit(Doc, kind="create")
    def h(ev):
        seen.append(ev.record.id)

    r = Doc(n=1)
    s = Session(store.engine, future=True)
    uow = UnitOfWork(s, owns_commit=False)
    with uow:
        uow.stage(Event(kind="create", record=r))
        assert seen == []
    s.rollback()
    s.close()
    assert seen == []  # NEVER fired


def test_nested_uow_never_commits(store):
    from eventic.record import Record

    class Doc(Record, stream="uow_nested_proxy"):
        pass

    with store.unit_of_work() as outer:
        inner = store.unit_of_work()
        assert inner is not outer
        assert inner.session is outer.session
