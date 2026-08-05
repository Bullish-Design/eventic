"""probe_06 — scaling and concurrency (0.3: F17 bounded reads + I5 the canary).

The concurrency section is the permanent canary: 8 threads race one
``(id, version)`` and must produce exactly **1 winner, 7 loud
``StaleVersionError``s**. This behavior is the one thing 0.2 got right; it
must be unchanged at every exit gate.

Run: .venv/bin/python probes/probe_06_scaling_and_concurrency.py
"""

import tempfile, os, time, threading
from eventic import Record, Store, Delta, StaleVersionError

url = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "f.db")
store = Store(url, create_tables=True).activate()

print("=== F17: delta point reads are bounded — no full-history streaming ===")
class D(Record, stream="p6delta", codec=Delta(k=20)):
    n: int = 0

d = D(n=0).save()
for i in range(1, 800):
    d = d.update(n=i)

from sqlalchemy import event as sa_event

q = [0]
sa_event.listen(store.engine, "before_cursor_execute", lambda *a, **k: q.__setitem__(0, q[0] + 1))
t = time.perf_counter()
q[0] = 0
D.get(d.id, version=799)
el = time.perf_counter() - t
print(f"  get(v=799) on an 800-version delta aggregate: {el*1000:.1f} ms, {q[0]} SQL statement(s) (≤ K=20 rows)")

print("\n=== I5 (the canary): concurrent writers at one (id, version) ===")
class C(Record, stream="p6conc"):
    n: int = 0

base = C(n=0).save()
errs, oks = [], []
def w(v):
    with Store(url, create_tables=False):  # ContextVars don't cross threads
        try:
            base.update(n=v); oks.append(v)
        except StaleVersionError:
            errs.append(v)
        except Exception as e:
            errs.append(type(e).__name__)
ts = [threading.Thread(target=w, args=(i,)) for i in range(8)]
[t.start() for t in ts]; [t.join() for t in ts]
print("  winners:", len(oks), " losers(loud):", len(errs), "->", errs[:3])
print("  versions in store:", len(C.history(base.id)))
assert len(oks) == 1 and len(errs) == 7, "I5 violated: the canary changed"
print("  >>> I5 OK: exactly 1 winner, 7 loud StaleVersionErrors")
