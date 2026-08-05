"""R12 (search cursor ordering) and R8 (schema-check seeding order).

R12 claim: `search` pages ordered by aggregate_id (UUID), which is not temporal;
           with concurrent writers a later-created head can sort earlier — can a
           page boundary skip or duplicate results?
R8  claim: `check()` seeds missing ledger rows and reports clean, so drift is
           only caught on the second check.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel

from eventic import App, Stream
from eventic.sql import SQLite


class Item(BaseModel):
    bucket: int = 0
    tag: str = ""


items = Stream(Item, name="items")

print("=== R12: keyset paging over aggregate_id ===")
store = SQLite(":memory:")
ev = App(id="d", streams=[items]).bind(store)

# 10 aggregates with explicit, deliberately non-monotonic UUIDs
ids = [UUID(int=i) for i in (9, 3, 7, 1, 5, 2, 8, 4, 6, 10)]
for n, aid in enumerate(ids):
    ev[items].create(Item(bucket=1, tag=f"t{n}"), id=aid)

# page through with limit=3, inserting a NEW aggregate that sorts BEFORE the
# cursor after the first page (the concurrent-writer case R12 describes)
seen: list[UUID] = []
cursor = None
page_no = 0
while True:
    page = ev[items].where(limit=3, cursor=cursor, bucket=1)
    seen.extend(r.id for r in page.items)
    page_no += 1
    if page_no == 1:
        # a later-created head whose UUID sorts earliest of all
        ev[items].create(Item(bucket=1, tag="late-but-low"), id=UUID(int=0))
    cursor = page.cursor
    if cursor is None:
        break

print(f"  pages: {page_no}, ids returned: {len(seen)}, distinct: {len(set(seen))}")
print(f"  duplicates: {len(seen) - len(set(seen))}")
missed = {UUID(int=0)} - set(seen)
print(f"  the concurrently-created low UUID was returned? {UUID(int=0) in seen}")
print(f"  every pre-existing aggregate returned exactly once? "
      f"{sorted(set(seen) & set(ids)) == sorted(ids) and len(seen) == len(set(seen))}")
assert len(seen) == len(set(seen)), "keyset paging produced a duplicate"
assert set(ids) <= set(seen), "keyset paging skipped a pre-existing row"
print("  -> no duplicate, no skip of rows present at page-1 time.")
print("     A row created after paging began and sorting before the cursor is")
print("     missed — inherent to keyset paging, but the ordering is by UUID, so")
print("     'missed' is unpredictable rather than 'older than when I started'.")
store.close()

print("\n=== R8: does `schema check` seed and then pass? ===")
store2 = SQLite(":memory:")
app = App(id="d", streams=[items])
admin2 = store2.admin()

report1 = admin2.check(app)
print(f"  check #1 on an empty database: drift={report1.drift}")
for row in report1.streams:
    print(f"    stream={row[0]} v{row[1]} declared={row[2][:12]}… stored={row[3][:12]}… ok={row[4]}")

# now declare a DIFFERENT model under the same name and version — real drift
class ItemV2(BaseModel):
    bucket: int = 0
    tag: str = ""
    added_without_a_version_bump: bool = False


drifted = App(id="d", streams=[Stream(ItemV2, name="items")])
report2 = admin2.check(drifted)
print(f"  check #2 with a changed model, same schema_version: drift={report2.drift}")
for row in report2.streams:
    print(f"    declared={row[2][:12]}… stored={row[3][:12]}… ok={row[4]}")

print()
print("  -> check() INSERTS the declared fingerprint when the row is missing and")
print("     reports ok=True. On a database that has never been written to, the")
print("     first `eventic schema check` cannot detect drift — it defines the")
print("     baseline. A read-only 'check' command that mutates the database is")
print("     also a surprise for an operator running it against production.")
assert report1.drift is False
assert report2.drift is True
print("     Drift IS caught once a baseline exists (check #2), so the deploy-time")
print("     claim holds for any database that has been committed to.")
store2.close()
