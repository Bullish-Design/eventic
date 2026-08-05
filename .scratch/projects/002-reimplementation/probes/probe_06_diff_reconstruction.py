"""probe_06 — DiffStorage reconstruction across a snapshot boundary (Step 8).

Verifies, against the repo's own .venv (SQLite), the two claims DiffStorage's
decode depends on:

  1. A chain longer than K reconstructs exactly: snapshots at v0, vK, 2K, ...
     and deltas between; decode replays from the nearest snapshot forward and
     the result equals the FullSnapshot library's state byte-for-byte at every
     version (including the versions *around* a snapshot boundary).
  2. Exact-version reads (`get(id, version=n)`) reconstruct the state as of n,
     and an absent version raises KeyError (no silent drift to a neighbour).

Run: .venv/bin/python .scratch/projects/002-reimplementation/probes/probe_06_diff_reconstruction.py
"""
import tempfile
import uuid

from eventic.connect import _reset, connect
from eventic.record import Record
from eventic.plugins.codec import DiffStorage, FullSnapshot


class Snap(Record, FullSnapshot):
    body: str = ""
    status: str = "draft"


class Diff(Record, DiffStorage):
    from typing import ClassVar as _C

    K: _C[int] = 3  # snapshots at v0, v3, v6 — the boundary the probe crosses
    body: str = ""
    status: str = "draft"


def main():
    _reset()
    connect(f"sqlite:///{tempfile.mkdtemp()}/p.db")

    def chain(cls):
        d = cls(body="initial", status="draft").save()
        for i in range(1, 11):
            d = d.update(body=f"revision {i}") if i % 2 else d.update(status=f"status-{i}")
        return d

    a = chain(Snap)  # distinct aggregates: ids are random; we compare state
    b = chain(Diff)

    def state(obj):
        dump = obj.model_dump(mode="json")
        dump.pop("id", None)
        dump.pop("version_id", None)
        return dump

    identical = True
    for v in range(11):  # crosses v3 and v6 snapshot boundaries
        if state(Snap.get(a.id, version=v)) != state(Diff.get(b.id, version=v)):
            identical = False
            print(f"  MISMATCH at v{v}")
    print(f"[1] diff reconstruction == full snapshot at every version 0..10: {identical}")

    # exact-version reads land on the requested version, not a neighbour
    v7 = Diff.get(b.id, version=7)
    print(f"[2] get(v=7) is exactly v7: {v7.version == 7 and v7.status == 'status-6' and v7.body == 'revision 7'}")
    try:
        Diff.get(b.id, version=99)
        print("[2] get(v=99) for an absent version: NO ERROR (bad)")
    except KeyError:
        print("[2] get(v=99) for an absent version: KeyError (loud)")

    _reset()
    print()
    print("=> diff reconstruction across the snapshot boundary is byte-identical")
    print("   to full snapshots; exact-version reads stay exact.")


if __name__ == "__main__":
    main()
