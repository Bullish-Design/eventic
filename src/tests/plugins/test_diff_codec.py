"""DiffStorage codec tests (Step 8 — the second real plugin, R-P1/R-P2).

Verifies: encode→decode roundtrips across snapshot boundaries; reconstruction
at arbitrary versions matches the FullSnapshot library byte-for-byte; the
storage size win for large aggregates; the ``DiffStorage + TypedTable``
guardrail (MissingCapability at definition); and codec-aware ``where()``
matching the true reconstructed head (D14).
"""

import json

import pytest

from sqlalchemy import select
from sqlalchemy.orm import Session

from eventic.connect import _reset, connect, engine
from eventic.models import RecordRow
from eventic.errors import MissingCapability
from eventic.plugins.codec import DiffStorage, FullSnapshot
from eventic.plugins.persistence import TypedTable
from eventic.record import Record


class Snap(Record, FullSnapshot):
    body: str = ""
    status: str = "draft"


class Diff(Record, DiffStorage):
    from typing import ClassVar as _C

    K: _C[int] = 3  # snapshot at v0, v3, v6, ...
    body: str = ""
    status: str = "draft"


@pytest.fixture()
def db(tmp_path):
    _reset()
    connect(f"sqlite:///{tmp_path / 'e.db'}")
    yield
    _reset()


def _edit_chain(cls, n, rec_id=None):
    """Create a record and edit it n times (alternating fields)."""
    kwargs = {"id": rec_id} if rec_id is not None else {}
    d = cls(body="initial", status="draft", **kwargs).save()
    for i in range(1, n + 1):
        if i % 2:
            d = d.update(body=f"revision {i}")
        else:
            d = d.update(status=f"status-{i}")
    return d


def test_diff_roundtrip_across_snapshot_boundary(db):
    d = _edit_chain(Diff, 7)  # snapshots at 0, 3, 6; deltas between
    got = Diff.get(d.id)
    assert got.version == 7
    assert got.body == "revision 7"
    assert got.status == "status-6"

    hist = Diff.history(d.id)
    assert [h.version for h in hist] == list(range(8))
    assert hist[2].body == "revision 1" and hist[2].status == "status-2"
    assert hist[3].body == "revision 3"  # v3 is a snapshot (3 % 3 == 0)


def test_diff_matches_full_snapshot_byte_for_byte(db):
    """Same edit sequence under both codecs -> identical reconstructed state
    at every version (identity fields excluded: the aggregates are distinct)."""
    import uuid as _uuid

    a = _edit_chain(Snap, 7, _uuid.uuid4())
    b = _edit_chain(Diff, 7, _uuid.uuid4())

    def state(obj):
        dump = obj.model_dump(mode="json")
        dump.pop("id", None)
        dump.pop("version_id", None)
        return dump

    for v in range(8):
        assert state(Snap.get(a.id, version=v)) == state(Diff.get(b.id, version=v)), \
            f"version {v} diverged"


def test_diff_storage_size_win(db):
    """A large body edited N times stores far less than N full snapshots."""
    big = "x" * 50_000
    d = Diff(body=big).save()
    for i in range(1, 6):  # v1..v5: only two delta rows before the K=3 snapshot
        d = d.update(body=big, status=f"s{i}") if i == 3 else d.update(status=f"s{i}")
    with Session(engine()) as s:
        rows = s.execute(
            select(RecordRow.data).where(RecordRow.id == d.id).order_by(RecordRow.version)
        ).scalars().all()
    total = sum(len(json.dumps(r)) for r in rows)
    # delta rows store only the changed field(s): 5 updates with a 50KB body
    # that only changes `status` -> at most one full snapshot + tiny deltas
    assert total < 200_000, f"diff storage too big: {total} bytes"
    snap_bytes = len(json.dumps(d.model_dump(mode="json")))
    assert total < 5 * snap_bytes  # ≪ N×body


def test_diff_plus_typedtable_fails_at_definition():
    """The guardrail: DiffStorage needs persistence:json, TypedTable doesn't
    provide it, and the REPLACED default's capabilities don't count (D7)."""
    with pytest.raises(MissingCapability):

        class Broken(Record, DiffStorage, TypedTable):
            pass


def test_where_matches_reconstructed_head(db):
    """where() on a diff class matches the true head, not a delta row (D14)."""
    a = Diff(body="x", status="published").save()
    b = Diff(body="y", status="draft").save()
    # flip a's status AFTER its K=3 snapshot -> the head is a delta row
    a = a.update(status="draft")
    assert a.version == 1
    got = Diff.where(status="draft")
    assert {r.id for r in got} == {a.id, b.id}
    # and the head-state match reflects the delta-updated status
    assert Diff.get(a.id).status == "draft"


def test_diff_k_tunable_per_subclass(db):
    from typing import ClassVar

    class K2(Record, DiffStorage):
        K: ClassVar[int] = 2  # ClassVar: pydantic must not treat K as a field

    d = K2(body="a").save()
    d = d.update(body="b")  # v1: delta (1 % 2 != 0)
    d = d.update(body="c")  # v2: snapshot (2 % 2 == 0)
    assert K2.get(d.id).body == "c"
    with Session(engine()) as s:
        kinds = s.execute(
            select(RecordRow.data).where(RecordRow.id == d.id).order_by(RecordRow.version)
        ).scalars().all()
    assert [k["kind"] for k in kinds] == ["snapshot", "delta", "snapshot"]
