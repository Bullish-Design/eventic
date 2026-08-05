"""SqlStore: append/I5, head projection, search pushdown, outbox staging,
and the schema (indexes, F18).
"""

import uuid

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from eventic import Record
from eventic.errors import StaleVersionError
from eventic.identity import version_id
from eventic.store.schema import HeadRow, LogRow, OutboxRow


class Doc(Record, stream="sql_doc"):
    title: str | None = None
    body: str | None = None


# --------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------- #
def test_schema_triad_exists(store):
    names = set(inspect(store.engine).get_table_names())
    assert {"eventic_log", "eventic_head", "eventic_outbox"} <= names


def test_indexes(store):
    """F18: one composite index (stream leading) + the partial snapshot index;
    no per-id overlap."""
    insp = inspect(store.engine)
    log = {i["name"] for i in insp.get_indexes("eventic_log")}
    assert "ix_eventic_log_stream_id_version" in log
    assert "ix_eventic_log_snapshot" in log
    uq = {c["name"] for c in insp.get_unique_constraints("eventic_log")}
    assert "uq_eventic_log_id_version" in uq


# --------------------------------------------------------------------- #
# append + I5
# --------------------------------------------------------------------- #
def _count(store):
    with Session(store.engine) as s:
        return len(s.execute(select(LogRow)).scalars().all())


def test_append_inserts_row(store):
    d = Doc(title="a").save()
    with Session(store.engine) as s:
        row = s.execute(select(LogRow).where(LogRow.id == d.id)).scalar_one()
    assert row.version == 0
    assert row.kind == "create"
    assert row.snapshot is True
    assert row.data == {"title": "a", "body": None, "meta": {}}
    assert row.version_id == version_id(d.id, 0)
    assert row.committed_at is not None
    assert _count(store) == 1


def test_replay_is_silent_noop(store):
    d = Doc(title="x").save()
    d.save()
    assert _count(store) == 1


def test_different_writer_raises(store):
    base = Doc(title=None).save()
    a = Doc.get(base.id)
    b = Doc.get(base.id)
    a.update(title="A")
    with pytest.raises(StaleVersionError):
        b.update(title="B")
    assert Doc.get(base.id).title == "A"


# --------------------------------------------------------------------- #
# head projection
# --------------------------------------------------------------------- #
def test_head_tracks_log(store):
    d = Doc(title="a").save().update(title="b")
    with Session(store.engine) as s:
        head = s.execute(
            select(HeadRow).where(HeadRow.id == d.id)
        ).scalar_one()
    assert head.version == 1
    assert head.state["title"] == "b"
    assert head.committed_at is not None


def test_head_survives_rolled_back_transaction(store):
    d = Doc(title="a").save()
    with pytest.raises(RuntimeError):
        with store.unit_of_work():
            d.update(title="b")
            raise RuntimeError("abort")
    assert Doc.get(d.id).title == "a"
    assert Doc.get(d.id).version == 0


def test_head_matches_full_log_replay(store):
    d = Doc(title="a").save()
    for i in range(1, 5):
        d = d.update(title=f"rev {i}")
    from sqlalchemy import update

    from eventic.pipeline import rebuild_heads

    with Session(store.engine) as s:
        before = s.execute(select(HeadRow).where(HeadRow.id == d.id)).scalar_one()
        before_state, before_version = before.state, before.version
        s.execute(update(HeadRow).where(HeadRow.id == d.id).values(state={}))
        s.commit()
    rebuild_heads(store)
    with Session(store.engine) as s:
        after = s.execute(select(HeadRow).where(HeadRow.id == d.id)).scalar_one()
    assert after.state == before_state
    assert after.version == before_version


# --------------------------------------------------------------------- #
# search pushdown (F16)
# --------------------------------------------------------------------- #
def test_search_equality_and_dotted_paths(store):
    a = Doc(title="x", meta={"status": "published"}).save()
    b = Doc(title="y", meta={"status": "draft"}).save()
    c = Doc(title="z", meta={"status": "draft"}).save()
    assert {r.id for r in Doc.where(title="x")} == {a.id}
    assert {r.id for r in Doc.where(**{"meta.status": "draft"})} == {b.id, c.id}
    assert Doc.where(**{"meta.status": "deleted"}) == []


def test_search_normalizes_uuids(store):
    ref = uuid.uuid4()
    d = Doc(title="x", meta={"ref": ref}).save()
    found = Doc.where(**{"meta.ref": ref})
    assert [r.id for r in found] == [d.id]


# --------------------------------------------------------------------- #
# outbox
# --------------------------------------------------------------------- #
def test_outbox_row_staged_inside_commit(store):
    from eventic import on_commit

    class Order(Record, stream="sql_order"):
        total: int = 0

    @on_commit(Order, via="outbox", queue="orders")
    def h(event):
        pass

    o = Order(total=5).save()
    with Session(store.engine) as s:
        rows = s.execute(select(OutboxRow)).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.stream == "sql_order"
    assert row.record_id == o.id
    assert row.version == 0
    assert row.queue == "orders"
    assert row.handler_id == f"{h.__module__}:{h.__qualname__}"


def test_outbox_rolls_back_with_the_version_row(store):
    from eventic import on_commit

    class Order(Record, stream="sql_order2"):
        total: int = 0

    @on_commit(Order, via="outbox", queue="orders")
    def h(event):
        pass

    with pytest.raises(RuntimeError):
        with store.unit_of_work():
            Order(total=1).save()
            raise RuntimeError("abort")
    with Session(store.engine) as s:
        n = len(s.execute(select(OutboxRow)).scalars().all())
    assert n == 0  # atomic: the outbox row died with the version row


def test_outbox_never_staged_for_non_opted_classes(store):
    """F10: a subscription on class A never touches class B's commits."""
    from eventic import on_commit

    class OptedIn(Record, stream="sql_opt"):
        n: int = 0

    class NotOptedIn(Record, stream="sql_not_opt"):
        n: int = 0

    @on_commit(OptedIn, via="outbox", queue="q")
    def h(event):
        pass

    OptedIn(n=1).save()
    NotOptedIn(n=2).save()
    with Session(store.engine) as s:
        streams = s.execute(select(OutboxRow.stream)).scalars().all()
    assert streams == ["sql_opt"]
