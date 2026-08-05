"""probe_04 — I7 violation and query costs (0.3: F3/F11/F16).

Run: .venv/bin/python probes/probe_04_i7_violation_and_costs.py
"""

import tempfile, os
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session

from eventic import Record, Store, Interceptor, on_commit
from eventic.store.unit_of_work import UnitOfWork

url = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "d.db")
store = Store(url, create_tables=True).activate()

print("=== F3: I7 — do sync handlers fire before durability? ===")
class Order(Record, stream="p4order"):
    total: int = 0

fired = []
@on_commit(Order, kind="create")
def h(ev):
    fired.append(ev.record.total)

s = Session(store.engine, future=True)   # a foreign session (e.g. a DBOS txn)
with UnitOfWork(s, owns_commit=False):
    Order(total=99).save()
    print("  handler fired while tx still open:", fired, " <-- VIOLATION" if fired else "ok (not yet durable)")
s.rollback(); s.close()
print("  handler fired for a rolled-back version:", fired, " <-- I7 VIOLATED" if fired else "ok")

print("\n=== F11: before_commit's return value is threaded ===")
class Enrich(Interceptor):
    def before_commit(self, record):
        return record.model_copy(update={"n": 4242})

class Enriched(Record, stream="p4enr", interceptors=(Enrich(),)):
    n: int = 0

e = Enriched(n=1).save()
print("  stored n =", Enriched.get(e.id).n, " <-- return value discarded?" if Enriched.get(e.id).n == 1 else "ok")

print("\n=== F16: where() pushes down — no N+1 ===")
class Big(Record, stream="p4big"):
    n: int = 0

for i in range(10):
    b = Big(n=i).save()
    for _ in range(8):
        b = b.update(n=b.n + 100)
    b = b.update(n=900)

q = [0]
sa_event.listen(store.engine, "before_cursor_execute", lambda *a, **k: q.__setitem__(0, q[0] + 1))
q[0] = 0
hits = Big.where(n=900)
print("  where() on 10 aggregates x10 versions -> %d SQL statement(s), %d hit(s)" % (q[0], len(hits)))

print("\ndone — probe_04 reports corrected behavior.")
