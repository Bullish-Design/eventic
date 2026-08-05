"""fold_properties_into_data

Fold the 0.1 ``properties`` column into ``data.meta`` and drop the column
(CONCEPT/REWRITE-GUIDE Step 10). Reversible.

- Postgres: ``jsonb_set`` fold + ``DROP COLUMN``.
- SQLite: a data-level ``json_set`` fold, then Alembic's table-rebuild to drop
  the column (SQLite cannot ``DROP COLUMN`` natively... as of the toolchains
  target; the batch rebuild is portable).

The C6 backfill (a1b2c3d4e5f6) runs BEFORE this revision, so pre-0.2
duplicates are deduped and the ``(id, version)`` unique constraint exists
before any rows are folded.

Revision ID: fold_properties_into_data
Revises: a1b2c3d4e5f6
Create Date: 2026-08-04
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "fold_properties_into_data"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                "UPDATE records SET data = jsonb_set("
                "data, '{meta}', COALESCE(properties, '{}'::jsonb))"
            )
        )
        op.execute(sa.text("ALTER TABLE records DROP COLUMN properties"))
    else:
        # SQLite: fold first (json_set is data-level), then rebuild to drop
        # the column (Alembic batch mode recreates the table, preserving the
        # pk, the unique constraint, and the id index).
        op.execute(
            sa.text(
                "UPDATE records SET data = json_set("
                "data, '$.meta', json(COALESCE(properties, '{}')))"
            )
        )
        with op.batch_alter_table("records") as batch_op:
            batch_op.drop_column("properties")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                "ALTER TABLE records "
                "ADD COLUMN properties JSONB NOT NULL DEFAULT '{}'::jsonb"
            )
        )
        op.execute(
            sa.text(
                "UPDATE records SET properties = data->'meta' "
                "WHERE data->'meta' IS NOT NULL"
            )
        )
    else:
        # SQLite ADD COLUMN supports a constant default; backfill from data.
        op.execute(
            sa.text(
                "ALTER TABLE records "
                "ADD COLUMN properties JSON NOT NULL DEFAULT '{}'"
            )
        )
        op.execute(
            sa.text(
                "UPDATE records SET properties = json_extract(data, '$.meta') "
                "WHERE json_extract(data, '$.meta') IS NOT NULL"
            )
        )
