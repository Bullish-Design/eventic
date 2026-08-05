"""probe_01 — Durable-v0 semantics, version_id asymmetry, and write amplification.

Claims under test (all against the repo's own .venv, SQLite):

  1. Constructing a Record writes a row to the DB *immediately* (durable v0).
     -> "just build a model" is a DB write. Footgun for tests/validation/scratch.
  2. v0's version_id is RANDOM (uuid4) while every mutation's version_id is
     DETERMINISTIC (uuid5). The README claims deterministic ids "for every
     mutation"; v0 is the asymmetric exception -> replay-idempotency of
     construction relies on transaction atomicity, not on the key.
  3. Write amplification: count the INSERT INTO records statements produced by a
     realistic edit session (construct + 2 field edits + 1 props.add).
  4. The L4 no-op guard still runs a FULL model re-validation (constructs a whole
     new object) before deciding not to write.

Run: .venv/bin/python .scratch/projects/002-reimplementation/probes/probe_01_durable_v0_and_amplification.py
"""
import tempfile, os, uuid
from sqlalchemy import event, text
from sqlalchemy.engine import Engine

INSERTS = []


@event.listens_for(Engine, "before_cursor_execute")
def _count(conn, cursor, statement, params, context, executemany):
    s = " ".join(statement.split())
    if "INSERT INTO records" in s:
        INSERTS.append(s)


from eventic import Eventic, Record


class Story(Record):
    title: str | None = None
    body: str | None = None


def rows(engine):
    with engine.connect() as c:
        return c.execute(text("SELECT id, version, version_id FROM records ORDER BY version")).fetchall()


def main():
    tmp = tempfile.mkdtemp()
    Eventic.init(name="probe01", database_url=f"sqlite:///{tmp}/p.db")
    Eventic.launch()
    engine = Record._store.engine
    try:
        # (1) construction writes v0 immediately
        INSERTS.clear()
        s = Story(title="hello")
        print(f"[1] rows in DB immediately after Story(...):        {len(rows(engine))}  "
              f"(INSERTs fired: {len(INSERTS)})")
        assert len(rows(engine)) == 1, "v0 not persisted at construction"

        # (2) version_id asymmetry.  (SQLite stores Uuid as unhyphenated hex, so
        #     normalize both sides to .hex before comparing.)
        def h(v):
            return uuid.UUID(str(v)).hex
        v0_vid = rows(engine)[0].version_id
        s.body = "b"  # -> v1
        all_rows = rows(engine)
        v1_vid = [r for r in all_rows if r.version == 1][0].version_id
        expected_v1 = uuid.uuid5(uuid.NAMESPACE_URL, f"eventic:{s.id}:1").hex
        expected_v0 = uuid.uuid5(uuid.NAMESPACE_URL, f"eventic:{s.id}:0").hex
        v0_det = h(v0_vid) == expected_v0
        v1_det = h(v1_vid) == expected_v1
        print(f"[2] v0 version_id deterministic? {v0_det}   v1 version_id deterministic? {v1_det}")
        print(f"    -> v0 id is {'deterministic' if v0_det else 'RANDOM (uuid4)'}, "
              f"v1 id is {'deterministic (uuid5)' if v1_det else 'RANDOM'}")

        # (3) write amplification for a realistic session
        INSERTS.clear()
        s2 = Story(title="draft")          # v0
        s2.title = "final title"           # v1
        s2.body = "the body"               # v2
        s2.properties.add(status="published")  # v3
        print(f"[3] realistic edit session (construct + 2 edits + 1 props.add):")
        print(f"    INSERT INTO records statements fired: {len(INSERTS)}  "
              f"(one full-row insert per version; each also runs full model validation)")
        assert len(INSERTS) == 4

        # (4) no-op guard still fully re-validates
        INSERTS.clear()
        before = len(rows(engine))
        s2.title = "final title"  # identical value -> no-op (no INSERT), but new_obj built
        after = len(rows(engine))
        print(f"[4] no-op assignment wrote a row? {after > before}  "
              f"(INSERTs: {len(INSERTS)}) -- but a whole new Story was still constructed+validated "
              f"to discover it was a no-op")
    finally:
        Eventic.destroy()
        Eventic.reset()


if __name__ == "__main__":
    main()
