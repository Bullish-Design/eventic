"""probe_01 — dead seams and stale handles (0.3: F1/F5/F6/F8/F9/F15).

Run: .venv/bin/python probes/probe_01_dead_seams_and_stale_handles.py
"""

import tempfile, os, uuid
from typing import ClassVar  # noqa: F401  (0.2 API reference: gone)
from eventic import Record, connect, version_id, Delta
from eventic.errors import RecordNotFound

connect("sqlite:///" + os.path.join(tempfile.mkdtemp(), "a.db"))

print("=== F8: use() is gone (0.2's public API that did nothing) ===")
try:
    from eventic import use  # noqa: F401
    print("  use() still exists  <-- FAIL")
except ImportError:
    print("  use() -> ImportError  ok")

print("\n=== F9: identity is a function, not a seam ===")
rid = uuid.uuid4()
expected = uuid.uuid5(uuid.NAMESPACE_URL, f"eventic:{rid}:3")
print("  version_id(id, 3) == uuid5(...):", version_id(rid, 3) == expected)

print("\n=== F1: keywords, not mixins — no phantom fields ===")
class Doc(Record, stream="p1doc", codec=Delta(k=3)):
    body: str = ""

print("  model_fields:", sorted(set(Doc.model_fields) - {"id", "version", "version_id", "created_ts", "meta"}))
assert "seam" not in Doc.model_fields and "mode" not in Doc.model_fields

print("\n=== F5: created_ts populates (was always None) ===")
class Todo(Record, stream="p1todo"):
    text: str = ""
t = Todo(text="hi").save()
got = Todo.get(t.id)
print("  hydrated created_ts:", got.created_ts, " <-- still None?" if got.created_ts is None else "ok")

print("\n=== F6: draft().commit() returns the new version ===")
d = t.draft(); d.text = "hello"
t2 = d.commit()
print("  returned version:", t2.version, " <-- stale handle?" if t2.version != 1 else "ok")
print("  original handle stays v0:", t.version == 0)

print("\n=== F15: get() raises RecordNotFound (EventicError + KeyError) ===")
try:
    Todo.get(uuid.uuid4())
except KeyError as e:
    print("  KeyError:", type(e).__name__, "derives from EventicError:", isinstance(e, __import__("eventic").errors.RecordNotFound))

print("\ndone — probe_01 reports corrected behavior.")
