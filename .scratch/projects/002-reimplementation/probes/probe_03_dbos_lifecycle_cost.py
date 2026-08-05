"""probe_03 — What DBOS costs the dev/test experience.

Eventic makes DBOS a MANDATORY dependency and a process singleton. Even the
"minimal script, no web server" path and every single unit test pay for a full
DBOS init + launch + system-database migration + queue-manager thread pool.

This probe times that overhead and counts the DBOS system tables created in the
SQLite system DB, to quantify what the substrate costs for the 80% use case
(store a versioned pydantic model; read it back).

Run: .venv/bin/python .scratch/projects/002-reimplementation/probes/probe_03_dbos_lifecycle_cost.py
"""
import tempfile, time, logging
logging.disable(logging.CRITICAL)  # silence DBOS migration chatter for clean timing
from sqlalchemy import create_engine, text
from eventic import Eventic, Record


class Doc(Record):
    title: str | None = None


def main():
    tmp = tempfile.mkdtemp()
    url = f"sqlite:///{tmp}/p.db"

    t0 = time.perf_counter()
    Eventic.init(name="probe03", database_url=url)
    t1 = time.perf_counter()
    Eventic.launch()
    t2 = time.perf_counter()

    # count DBOS's own tables vs eventic's one table in the system/app DB
    eng = create_engine(url)
    with eng.connect() as c:
        tbls = [r[0] for r in c.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))]
    dbos_tbls = [t for t in tbls if t != "records" and not t.startswith("sqlite_")]

    # cost of the actual value delivered: one versioned write + read-back
    t3 = time.perf_counter()
    d = Doc(title="hello")
    _ = Doc.hydrate(d.id)
    t4 = time.perf_counter()

    t5 = time.perf_counter()
    Eventic.destroy()
    Eventic.reset()
    t6 = time.perf_counter()

    print(f"Eventic.init()          : {(t1-t0)*1000:8.1f} ms")
    print(f"Eventic.launch()        : {(t2-t1)*1000:8.1f} ms")
    print(f"construct v0 + hydrate  : {(t4-t3)*1000:8.1f} ms   <- the actual value delivered")
    print(f"destroy()+reset()       : {(t6-t5)*1000:8.1f} ms")
    print(f"TOTAL lifecycle overhead: {((t2-t0)+(t6-t5))*1000:8.1f} ms per process/test")
    print()
    print(f"tables in the DB: {len(tbls)} total")
    print(f"  eventic's own : ['records']  (1)")
    print(f"  DBOS system   : {len(dbos_tbls)} tables -> {dbos_tbls}")


if __name__ == "__main__":
    main()
