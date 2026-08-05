"""Backfill for pre-0.2 deployments (C6): dedupe + unique (id, version).

Existing `records` tables (created before Eventic 0.2) have no
`uq_records_id_version` constraint and may contain duplicate
`(id, version)` rows from the pre-C6 concurrency bug.

This migration is a **no-op on fresh installs** (the initial revision already
creates the constraint and there are no duplicates), so `alembic upgrade head`
is safe everywhere.

Step 1 (dedupe) is destructive: it keeps the row with the highest version_id
per (id, version). Review it on a staging copy first.
Step 2 adds the unique constraint on Postgres if it is missing. SQLite cannot
`ALTER TABLE ... ADD CONSTRAINT`, so legacy SQLite databases must be rebuilt
from the initial migration instead.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "6725d5d5ed38"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEDUPE_SQL = """
DELETE FROM records
WHERE version_id NOT IN (
    SELECT MAX(version_id) FROM records GROUP BY id, version
)
"""


def upgrade() -> None:
    # 1. Dedupe: keep the latest version_id per (id, version).
    #    Portable across Postgres and SQLite; no-op when there are no
    #    duplicates (the subquery always contains every surviving row).
    op.execute(sa.text(DEDUPE_SQL))

    # 2. Add the unique constraint on Postgres if missing.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        exists = bind.execute(
            sa.text(
                "SELECT 1 FROM pg_constraint WHERE conname = 'uq_records_id_version'"
            )
        ).scalar()
        if not exists:
            op.create_unique_constraint(
                "uq_records_id_version", "records", ["id", "version"]
            )


def downgrade() -> None:
    # The dedupe DELETE is not reversible (rows were intentionally removed).
    # Nothing to do; on fresh installs the constraint is owned by the initial
    # revision and drops with it.
    pass
