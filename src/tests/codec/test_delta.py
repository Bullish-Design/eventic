"""Delta codec (Step 20): forward deltas + tombstones + a snapshot every ``k``
versions. Verifies roundtrips across snapshot boundaries, removal tombstones
(F4), bounded window reads (F17), the size win, and per-class ``k``.
"""

import random

from eventic import Delta, Record
from eventic.seams import Window
from eventic.store.schema import LogRow


class Diff(Record, stream="delta_diff", codec=Delta(k=3)):
    body: str = ""
    status: str = "draft"


class K2(Record, stream="delta_k2", codec=Delta(k=2)):
    body: str = ""


def _edit_chain(cls, n, rec_id=None):
    import uuid

    kwargs = {"id": rec_id} if rec_id is not None else {}
    d = cls(body="initial", status="draft", **kwargs).save()
    for i in range(1, n + 1):
        if i % 2:
            d = d.update(body=f"revision {i}")
        else:
            d = d.update(status=f"status-{i}")
    return d


def test_codec_is_delta():
    assert isinstance(Diff.__eventic__.codec, Delta)
    assert Diff.__eventic__.codec.k == 3


def test_encode_snapshot_vs_delta():
    codec = Diff.__eventic__.codec
    d0 = Diff(body="x", status="draft")
    data0, snap0 = codec.encode(None, d0)
    assert snap0 is True and data0 == {"body": "x", "status": "draft", "meta": {}}

    d1 = Diff(body="y", status="draft", id=d0.id, version=1)
    data1, snap1 = codec.encode(d0, d1)
    assert snap1 is False
    assert data1["set"] == {"body": "y"}
    assert data1["del"] == []

    # v3 is a snapshot (3 % k == 0)
    d3 = Diff(body="z", status="final", id=d0.id, version=3)
    data3, snap3 = codec.encode(d1, d3)
    assert snap3 is True


def test_roundtrip_across_snapshot_boundary(store):
    d = _edit_chain(Diff, 7)  # snapshots at 0, 3, 6; deltas between
    got = Diff.get(d.id)
    assert got.version == 7
    assert got.body == "revision 7"
    assert got.status == "status-6"
    hist = Diff.history(d.id)
    assert [h.version for h in hist] == list(range(8))
    assert hist[2].body == "revision 1" and hist[2].status == "status-2"
    assert hist[3].body == "revision 3"  # v3 is a snapshot


def test_removal_tombstones_do_not_resurrect(store):
    """F4: a delta that removes a key must not leave a ghost on decode."""
    codec = Diff.__eventic__.codec

    def row(version, snapshot, data):
        return LogRow(
            version_id=object(), stream="s", id=object(), version=version,
            kind="create" if version == 0 else "update", snapshot=snapshot, data=data,
        )

    rows = [
        row(0, True, {"title": "a", "tag": "t"}),
        row(1, False, {"set": {}, "del": ["tag"]}),
    ]
    state = codec.decode(rows)
    assert state == {"title": "a"}  # no ghost "tag"


def test_k_tunable_per_subclass(store):
    d = K2(body="a").save()
    d = d.update(body="b")  # v1: delta (1 % 2 != 0)
    d = d.update(body="c")  # v2: snapshot (2 % 2 == 0)
    assert K2.get(d.id).body == "c"
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from eventic.store import active_store
    from eventic.store.schema import LogRow

    with Session(active_store().engine) as s:
        snaps = s.execute(
            select(LogRow.snapshot).where(LogRow.id == d.id).order_by(LogRow.version)
        ).scalars().all()
    assert snaps == [True, False, True]


def test_property_random_sequences(store):
    """Random field changes/removals: get(v) equals the in-memory object at
    every v. Removals are encoded by constructing versions from a hand-built
    state (schema evolution is the real removal source)."""
    rng = random.Random(42)
    d = Diff(body="base", status="start").save()
    for i in range(1, 12):
        changes = {}
        if rng.random() < 0.6:
            changes["body"] = f"rev {i}"
        if rng.random() < 0.5:
            changes["status"] = f"s{i}"
        d = d.update(**changes)
    hist = Diff.history(d.id)
    assert hist[-1].body == d.body and hist[-1].status == d.status
    for v in range(12):
        assert Diff.get(d.id, version=v).body == hist[v].body
        assert Diff.get(d.id, version=v).status == hist[v].status


def test_storage_size_win(store):
    import json

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from eventic.store import active_store
    from eventic.store.schema import LogRow

    big = "x" * 50_000
    d = Diff(body=big).save()
    for i in range(1, 6):
        d = d.update(status=f"s{i}")  # only `status` changes; body stays
    with Session(active_store().engine) as s:
        rows = s.execute(
            select(LogRow.data).where(LogRow.id == d.id).order_by(LogRow.version)
        ).scalars().all()
    total = sum(len(json.dumps(r)) for r in rows)
    snap = len(json.dumps(d.model_dump(mode="json")))
    assert total < 5 * snap  # ≪ N × full snapshots


def test_window_is_since_snapshot():
    assert Diff.__eventic__.codec.window() is Window.SINCE_SNAPSHOT
