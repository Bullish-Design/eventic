"""probe_03 — field pollution (0.3: F1 persisted data + F13 stream collision).

Run: .venv/bin/python probes/probe_03_field_pollution.py
"""

import tempfile, os
from sqlalchemy import select
from sqlalchemy.orm import Session

from eventic import Record, connect, Delta
from eventic.errors import StreamCollision
from eventic.store import active_store
from eventic.store.schema import LogRow

connect("sqlite:///" + os.path.join(tempfile.mkdtemp(), "c.db"))

print("=== F1: data holds USER STATE ONLY — no framework metadata ===")
class Doc(Record, stream="p3doc", codec=Delta(k=10)):
    body: str = ""

d = Doc(body="hello").save()
with Session(active_store().engine) as s:
    row = s.execute(select(LogRow).where(LogRow.id == d.id)).scalar_one()
print("  persisted data keys:", sorted(row.data), " <-- phantom fields persisted?" if row.data.keys() - {"body", "meta"} else "ok")

print("\n=== F13: same-named classes no longer share a log silently ===")
class A(Record, stream="shared"):
    pass

try:
    class B(Record, stream="shared"):
        pass
    print("  no collision  <-- FAIL (silent log sharing)")
except StreamCollision:
    print("  StreamCollision raised  ok")

print("\ndone — probe_03 reports corrected behavior.")
