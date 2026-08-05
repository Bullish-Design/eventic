"""probe_02 — The "concurrency contract" is a silent lost-update.

The README (§"Concurrency contract") and test_concurrent_mutations_do_not_
duplicate_versions present deterministic version_id + ON CONFLICT DO NOTHING as
a *correctness* feature: "the collision is safely ignored ... so the history
never corrupts."

This probe shows the darker half of that behaviour: the exact same mechanism
that makes a *replay* idempotent also silently discards a genuinely different
concurrent write, AND leaves the losing in-memory object believing it succeeded.

Scenario: two readers of v0 edit DIFFERENT, independent fields. A merge is
logically possible; instead B's edit vanishes with no error.

Run: .venv/bin/python .scratch/projects/002-reimplementation/probes/probe_02_concurrency_silent_lost_update.py
"""
import tempfile
from eventic import Eventic, Record


class Doc(Record):
    title: str | None = None
    body: str | None = None


def main():
    tmp = tempfile.mkdtemp()
    Eventic.init(name="probe02", database_url=f"sqlite:///{tmp}/p.db")
    Eventic.launch()
    try:
        base = Doc(title=None, body=None)          # v0
        a = Doc.hydrate(base.id)                    # reader A sees v0
        b = Doc.hydrate(base.id)                    # reader B sees v0

        a.body = "IMPORTANT A DATA"                 # A writes v1 (id, version=1)
        b.title = "IMPORTANT B DATA"                # B derives (id, version=1) too...

        fresh = Doc.hydrate(base.id)
        print(f"[persisted v1] title={fresh.title!r}  body={fresh.body!r}")
        print(f"[in-memory  b] title={b.title!r}  body={b.body!r}  version={b.version}")
        print()
        b_lost = fresh.title != "IMPORTANT B DATA"
        b_thinks_it_won = b.title == "IMPORTANT B DATA"
        print(f"B's write silently lost from the DB?           {b_lost}")
        print(f"B's in-memory object still claims it succeeded? {b_thinks_it_won}")
        print(f"A merge of the two independent fields possible? True (title & body are disjoint)")
        print()
        print("=> The 'idempotency key' (id, version) cannot distinguish a crash-replay")
        print("   of the SAME write from TWO DIFFERENT concurrent writes. It treats the")
        print("   second as a duplicate and drops it. In a standalone script there is no")
        print("   SERIALIZABLE retry to save you: the data is just gone, no exception.")
        assert b_lost and b_thinks_it_won
    finally:
        Eventic.destroy()
        Eventic.reset()


if __name__ == "__main__":
    main()
