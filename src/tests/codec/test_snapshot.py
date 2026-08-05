"""Snapshot codec — the default: full validated user state per version."""

from eventic import Record, Snapshot
from eventic.seams import Window
from eventic.store.schema import LogRow


class Snap(Record, stream="snap_doc"):
    body: str = ""
    status: str = "draft"


def test_default_codec_is_snapshot():
    assert isinstance(Snap.__eventic__.codec, Snapshot)


def test_encode_is_full_user_state_only():
    d = Snap(body="x", status="draft")
    data, is_snapshot = Snap.__eventic__.codec.encode(None, d)
    assert is_snapshot is True
    assert data == {"body": "x", "status": "draft", "meta": {}}
    assert "id" not in data and "version" not in data and "created_ts" not in data


def test_decode_takes_last_row():
    codec = Snap.__eventic__.codec
    rows = [
        LogRow(version_id=object(), stream="s", id=object(), version=0,
               kind="create", snapshot=True, data={"body": "a"}),
        LogRow(version_id=object(), stream="s", id=object(), version=1,
               kind="update", snapshot=True, data={"body": "b"}),
    ]
    assert codec.decode(rows) == {"body": "b"}


def test_window_is_point():
    assert Snap.__eventic__.codec.window() is Window.POINT


def test_iter_states_is_a_straight_pass():
    rows = [
        LogRow(version_id=object(), stream="s", id=object(), version=0,
               kind="create", snapshot=True, data={"n": 0}),
        LogRow(version_id=object(), stream="s", id=object(), version=1,
               kind="update", snapshot=True, data={"n": 1}),
    ]
    states = [(s["n"], r.version) for s, r in Snap.__eventic__.codec.iter_states(rows)]
    assert states == [(0, 0), (1, 1)]


def test_roundtrip_end_to_end(store):
    d = Snap(body="initial").save()
    for i in range(1, 6):
        d = d.update(status=f"status-{i}")
    got = Snap.get(d.id)
    assert got.body == "initial" and got.status == "status-5"
    hist = Snap.history(d.id)
    assert [h.version for h in hist] == list(range(6))
    assert Snap.get(d.id, version=2).status == "status-2"
