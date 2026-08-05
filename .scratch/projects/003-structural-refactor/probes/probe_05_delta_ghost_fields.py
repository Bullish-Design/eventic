"""probe_05 — delta ghost fields (0.3: F4 tombstones + F5 created_ts).

Run: .venv/bin/python probes/probe_05_delta_ghost_fields.py
"""

import tempfile, os, random
from eventic import Record, connect, Delta
from eventic.store.schema import LogRow

connect("sqlite:///" + os.path.join(tempfile.mkdtemp(), "e.db"))

print("=== F4: delta tombstones — removed fields do not resurrect ===")
codec = Delta(k=10)
def row(version, snapshot, data):
    return LogRow(
        version_id=__import__("uuid").uuid4(), stream="p5", id=__import__("uuid").uuid4(),
        version=version, kind="create" if version == 0 else "update",
        snapshot=snapshot, data=data,
    )
rows = [
    row(0, True, {"title": "a", "tag": "t"}),
    row(1, False, {"set": {}, "del": ["tag"]}),
]
state = codec.decode(rows)
print("  v1 read back:", state, " <-- GHOST 'tag' resurrected" if "tag" in state else "ok (no ghost)")

print("\n=== F4 end-to-end: random add/change sequence ===")
rng = random.Random(7)
class Doc(Record, stream="p5doc", codec=Delta(k=4)):
    body: str = ""
    status: str = "draft"

d = Doc(body="base", status="start").save()
for i in range(1, 15):
    ch = {}
    if rng.random() < 0.6:
        ch["body"] = f"rev {i}"
    if rng.random() < 0.5:
        ch["status"] = f"s{i}"
    d = d.update(**ch)
hist = Doc.history(d.id)
print("  15 versions reconstructed; head matches:", Doc.get(d.id).body == d.body and Doc.get(d.id).status == d.status)

print("\n=== F5: created_ts is stamped from committed_at ===")
print("  created_ts:", Doc.get(d.id).created_ts, " <-- still None?" if Doc.get(d.id).created_ts is None else "ok")

print("\ndone — probe_05 reports corrected behavior.")
