"""Regression suite — one test per REVIEW.md finding (F1–F23) plus the I8
proxy (zero process-global state). This is the refactor's definition of done:

    pytest src/tests/regression -q   →   0 xfailed, 24 passed

Every test asserts the *correct* 0.3 behavior and is marked
``xfail(strict=True)`` until the matching step lands. ``strict=True`` means an
accidental fix shows up as an XPASS failure immediately rather than at the end.

Imports and class definitions live inside test bodies so this file also
collects cleanly against the pre-refactor codebase.
"""

import pathlib
import re
import uuid

import pytest


def _connect(tmp_path, name="r.db"):
    from eventic import connect

    return connect(f"sqlite:///{tmp_path / name}")


# --------------------------------------------------------------------- #
# Tier 1 — correctness and data integrity
# --------------------------------------------------------------------- #

def test_f01_phantom_fields_not_persisted(tmp_path):
    """Plugin mixins must not leak framework fields into user state/data."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from eventic import Record
    from eventic.codec.delta import Delta
    from eventic.store.schema import LogRow

    _connect(tmp_path)

    class Doc(Record, stream="f1doc", codec=Delta(k=10)):
        body: str = ""

    assert "seam" not in Doc.model_fields
    assert "provides" not in Doc.model_fields
    assert "requires" not in Doc.model_fields
    assert "priority" not in Doc.model_fields
    assert "mode" not in Doc.model_fields

    d = Doc(body="hello").save()
    from eventic.store import active_store

    with Session(active_store().engine) as s:
        row = s.execute(select(LogRow).where(LogRow.id == d.id)).scalar_one()
    assert set(row.data) == {"body", "meta"}  # user state only


def test_f02_subclassing_keeps_codec(tmp_path):
    """class SubDoc(Doc) inherits the codec and never crashes on required fields."""
    from eventic import Record
    from eventic.codec.delta import Delta

    _connect(tmp_path)

    class Doc(Record, stream="f2base", codec=Delta(k=5)):
        required: str

    class SubDoc(Doc):
        pass

    assert isinstance(SubDoc.__eventic__.codec, Delta)
    assert SubDoc.__eventic__.stream == "SubDoc"  # own stream, one per class
    s = SubDoc(required="x").save()
    assert SubDoc.get(s.id).required == "x"


def test_f03_events_not_fired_before_durability(tmp_path):
    """A sync handler must never fire for a version that gets rolled back."""
    from sqlalchemy.orm import Session

    from eventic import Record, on_commit
    from eventic.store.unit_of_work import UnitOfWork

    store = _connect(tmp_path)

    class Order(Record, stream="f3order"):
        total: int = 0

    fired = []

    @on_commit(Order, kind="create")
    def h(ev):
        fired.append(ev.record.total)

    s = Session(store.engine, future=True)
    with UnitOfWork(s, owns_commit=False):  # a foreign session (e.g. DBOS)
        Order(total=99).save()
    assert fired == []  # not durable yet
    s.rollback()
    s.close()
    assert fired == []  # NEVER fired — the version never became durable
    assert Order.where(total=99) == []


def test_f04_delta_tombstones_no_ghosts(tmp_path):
    """A removed field must not resurrect on read: deltas carry tombstones."""
    from eventic.codec.delta import Delta
    from eventic.store.schema import LogRow
    from sqlalchemy.orm import Session

    _connect(tmp_path)

    def _row(version, snapshot, data):
        return LogRow(
            version_id=uuid.uuid4(), stream="f4", id=uuid.uuid4(),
            version=version, kind="create" if version == 0 else "update",
            snapshot=snapshot, data=data,
        )

    rows = [
        _row(0, True, {"title": "a", "tag": "t"}),
        _row(1, False, {"set": {}, "del": ["tag"]}),
    ]
    state = Delta(k=10).decode(rows)
    assert state == {"title": "a"}  # no ghost "tag"


def test_f05_created_ts_populates(tmp_path):
    from eventic import Record

    _connect(tmp_path)

    class Todo(Record, stream="f5todo"):
        text: str = ""

    t = Todo(text="hi").save()
    got = Todo.get(t.id)
    assert got.created_ts is not None


def test_f06_draft_returns_new_version(tmp_path):
    from eventic import Record

    _connect(tmp_path)

    class Todo(Record, stream="f6todo"):
        text: str = ""

    t = Todo(text="a").save()
    d = t.draft()
    d.text = "b"
    t2 = d.commit()
    assert t2.version == 1  # the RETURNED value is the new version
    assert Todo.get(t2.id).text == "b"
    assert t.version == 0  # the original handle is untouched


def test_f07_broken_query_gone():
    """The broken SingleTableJSONB.query() dies with the plugins package."""
    with pytest.raises(ImportError):
        from eventic.plugins import persistence  # noqa: F401


# --------------------------------------------------------------------- #
# Tier 2 — architecture
# --------------------------------------------------------------------- #

def test_f08_use_gone():
    with pytest.raises(ImportError):
        from eventic import use  # noqa: F401


def test_f09_identity_is_a_function():
    from eventic.identity import version_id

    rid = uuid.uuid4()
    assert version_id(rid, 3) == uuid.uuid5(uuid.NAMESPACE_URL, f"eventic:{rid}:3")


def test_f10_delivery_is_per_subscription(tmp_path):
    """A subscription on class A must never touch class B's commits."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from eventic import Record, on_commit
    from eventic.store import active_store
    from eventic.store.schema import OutboxRow

    _connect(tmp_path)

    class OptedIn(Record, stream="f10a"):
        n: int = 0

    class NotOptedIn(Record, stream="f10b"):
        n: int = 0

    @on_commit(OptedIn, via="outbox", queue="q")
    def h(ev):
        raise AssertionError("must not be drained in this test")

    OptedIn(n=1).save()
    NotOptedIn(n=2).save()

    with Session(active_store().engine) as s:
        streams = s.execute(select(OutboxRow.stream)).scalars().all()
    assert streams == ["f10a"]  # only the opted-in class staged anything


def test_f11_before_commit_return_threaded(tmp_path):
    from eventic import Record, Interceptor

    _connect(tmp_path)

    class Enrich(Interceptor):
        def before_commit(self, record):
            return record.model_copy(update={"n": 4242})

    class Enriched(Record, stream="f11enr", interceptors=(Enrich(),)):
        n: int = 0

    e = Enriched(n=1).save()
    assert Enriched.get(e.id).n == 4242


def test_f12_dead_surface_gone():
    from eventic import EventicError, Veto

    assert issubclass(Veto, EventicError)  # Veto exported from the package root
    with pytest.raises(ImportError):
        from eventic.plugins import TypedTable  # noqa: F401


def test_f13_stream_collision_is_loud(tmp_path):
    from eventic import Record
    from eventic.errors import StreamCollision

    _connect(tmp_path)

    class A(Record, stream="shared"):
        pass

    with pytest.raises(StreamCollision):

        class B(Record, stream="shared"):
            pass


def test_f14_extra_forbid(tmp_path):
    from pydantic import ValidationError

    from eventic import Record

    _connect(tmp_path)

    class Todo(Record, stream="f14todo"):
        text: str = ""

    with pytest.raises(ValidationError):
        Todo(txt="typo")  # silently persisted forever in 0.2


def test_f15_not_found_is_eventic_and_keyerror(tmp_path):
    from eventic import Record
    from eventic.errors import EventicError, RecordNotFound

    _connect(tmp_path)

    class Doc(Record, stream="f15doc"):
        title: str | None = None

    assert issubclass(RecordNotFound, EventicError)
    assert issubclass(RecordNotFound, KeyError)  # both contracts hold
    with pytest.raises(KeyError):
        Doc.get(uuid.uuid4())


# --------------------------------------------------------------------- #
# Tier 3 — performance
# --------------------------------------------------------------------- #

def test_f16_where_is_not_n_plus_one(tmp_path):
    from sqlalchemy import event as sa_event

    from eventic import Record

    store = _connect(tmp_path)

    class Big(Record, stream="f16big"):
        n: int = 0

    ids = []
    for i in range(10):
        b = Big(n=i).save()
        for _ in range(8):
            b = b.update(n=b.n + 100)
        b = b.update(n=900)  # every head ends at n=900
        ids.append(b.id)

    q = [0]

    def _count(*a, **k):
        q[0] += 1

    sa_event.listen(store.engine, "before_cursor_execute", _count)
    q[0] = 0
    hits = Big.where(n=900)
    assert len(hits) == 10
    assert q[0] <= 2, f"where() issued {q[0]} SQL statements"
    sa_event.remove(store.engine, "before_cursor_execute", _count)


def test_f17_bounded_historical_reads(tmp_path):
    from eventic import Record
    from eventic.codec.delta import Delta
    from eventic.store.sql import SqlStore

    class CountingStore(SqlStore):
        def __init__(self):
            super().__init__()
            self.read_rows = 0

        def read(self, s, stream, rec_id, window, version):
            rows = super().read(s, stream, rec_id, window, version)
            self.read_rows += len(rows)
            return rows

    counting = CountingStore()
    _connect(tmp_path)

    class D(Record, stream="f17d", codec=Delta(k=20), rows=counting):
        n: int = 0

    d = D(n=0).save()
    for i in range(1, 800):
        d = d.update(n=i)
    counting.read_rows = 0
    got = D.get(d.id, version=799)
    assert got.n == 799
    assert counting.read_rows <= 20, "point read must not stream the full history"


def test_f18_indexes(tmp_path):
    from sqlalchemy import inspect

    store = _connect(tmp_path)
    insp = inspect(store.engine)
    names = {i["name"] for i in insp.get_indexes("eventic_log")}
    assert "ix_eventic_log_stream_id_version" in names
    assert "ix_eventic_log_snapshot" in names
    assert "records" not in insp.get_table_names()


def test_f19_history_linear_fold(tmp_path):
    from sqlalchemy import event as sa_event

    from eventic import Record

    store = _connect(tmp_path)

    class Big(Record, stream="f19big"):
        n: int = 0

    b = Big(n=0).save()
    for i in range(1, 300):
        b = b.update(n=i)

    q = [0]

    def _count(*a, **k):
        q[0] += 1

    sa_event.listen(store.engine, "before_cursor_execute", _count)
    q[0] = 0
    hist = Big.history(b.id)
    assert [h.n for h in hist] == list(range(300))  # v0..v299
    assert q[0] == 1, f"history() issued {q[0]} SQL statements (expect 1)"
    sa_event.remove(store.engine, "before_cursor_execute", _count)


# --------------------------------------------------------------------- #
# Tier 4 — hygiene
# --------------------------------------------------------------------- #

def test_f20_examples_do_not_connect_at_import():
    """A fresh interpreter: importing the webhook module must not build an
    app, must not connect a Store (subprocess, so no other test's binding can
    mask the assertion)."""
    import subprocess
    import sys

    code = """
import eventic.examples.webhook as wh
assert not hasattr(wh, 'app'), 'app built at import (F20)'
from eventic.store import active_store
from eventic.errors import NotConnected
try:
    active_store()
except NotConnected:
    pass
else:
    raise SystemExit('store active after import (F20)')
print('OK')
"""
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "OK" in out.stdout


def test_f21_annotations_resolve():
    import typing

    from eventic import subscribe

    typing.get_type_hints(subscribe.on_commit)  # must not raise NameError


def test_f22_handler_collision_is_loud(tmp_path):
    from eventic import Record, on_commit
    from eventic.errors import HandlerCollision

    _connect(tmp_path)

    class Doc(Record, stream="f22doc"):
        pass

    def h1(ev):
        pass

    h1.__qualname__ = "duplicate_handler"

    def h2(ev):
        pass

    h2.__qualname__ = "duplicate_handler"

    on_commit(Doc)(h1)
    with pytest.raises(HandlerCollision):
        on_commit(Doc)(h2)


def test_f23_connect_creates_tables_store_does_not(tmp_path):
    from sqlalchemy import inspect

    from eventic import Store, connect

    s = Store(f"sqlite:///{tmp_path / 'a.db'}")
    assert "eventic_log" not in inspect(s.engine).get_table_names()
    s.activate()

    c = connect(f"sqlite:///{tmp_path / 'b.db'}")  # dev sugar: DDL on
    assert "eventic_log" in inspect(c.engine).get_table_names()
    c.deactivate()
    s.deactivate()


# --------------------------------------------------------------------- #
# I8 — the mechanical proxy
# --------------------------------------------------------------------- #

def test_no_process_globals():
    """No mutable process state outside a Store: zero _reset_* hooks and
    zero module-level engine/registry globals in the package."""
    root = pathlib.Path(__file__).resolve().parents[2] / "eventic"
    offenders = []
    forbidden_globals = (
        "_ENGINE", "_ambient_session", "_GLOBAL_PLUGINS",
        "_DELIVERY_MODES", "_DELIVERY_INSTANCES",
    )
    for py in sorted(root.rglob("*.py")):
        text = py.read_text()
        for line in text.splitlines():
            stripped = line.strip()
            if re.match(r"def _reset\b|def _reset_", stripped):
                offenders.append(f"{py.relative_to(root)}: {stripped}")
            for name in forbidden_globals:
                if stripped.startswith(f"{name} =") or stripped.startswith(f"{name}:"):
                    offenders.append(f"{py.relative_to(root)}: {stripped}")
    assert offenders == []
