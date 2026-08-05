"""probe_02 — mixin MRO collapse (0.3: F2 subclassing + F10 per-subscription).

Run: .venv/bin/python probes/probe_02_mixin_mro_collapse.py
"""

import tempfile, os
from eventic import Record, connect, Delta, on_commit

connect("sqlite:///" + os.path.join(tempfile.mkdtemp(), "b.db"))

print("=== F2: subclassing a plugin-bearing Record keeps the codec ===")
class Doc(Record, stream="p2base", codec=Delta(k=3)):
    body: str = ""

class SubDoc(Doc, stream="p2sub"):
    pass

print("  Doc._codec   :", type(Doc.__eventic__.codec).__name__)
print("  SubDoc._codec:", type(SubDoc.__eventic__.codec).__name__, " <-- reverted?" if not isinstance(SubDoc.__eventic__.codec, Delta) else "")
print("  SubDoc stream:", SubDoc.__eventic__.stream)
s = SubDoc(body="works").save()
print("  subclass save/get:", SubDoc.get(s.id).body == "works")

print("\n=== F10: delivery is per-subscription, never process-wide ===")
class OptedIn(Record, stream="p2opt"):
    n: int = 0

class NotOptedIn(Record, stream="p2not"):
    n: int = 0

@on_commit(OptedIn, via="outbox", queue="q")
def spy(event):  # noqa: ARG001
    pass

OptedIn(n=1).save()
NotOptedIn(n=2).save()

from sqlalchemy import select
from sqlalchemy.orm import Session
from eventic.store import active_store
from eventic.store.schema import OutboxRow

with Session(active_store().engine) as s:
    streams = s.execute(select(OutboxRow.stream)).scalars().all()
print("  outbox streams staged:", streams, " <-- leaked to NotOptedIn?" if "p2not" in streams else "ok (only OptedIn)")

print("\ndone — probe_02 reports corrected behavior.")
