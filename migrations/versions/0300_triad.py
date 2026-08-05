"""0300 — rebuild from ``records`` to the log/head/outbox triad (Step 22).

Creates ``eventic_log`` / ``eventic_head`` / ``eventic_outbox``, copies every
``records`` row into ``eventic_log`` with the data unwrapped to the 0.3
shapes, builds the head projection by folding each stream, and drops
``records``.

Data unwrapping (both codecs, old → new):

- Old ``FullSnapshot`` rows: ``data`` was the full model dump — strip the
  managed keys (``id``/``version``/``version_id``/``created_ts``) and the
  phantom plugin keys (``seam``/``provides``/``requires``/``priority``/``mode``)
  so ``extra="forbid"`` never rejects your own historical rows on read.
- Old ``DiffStorage`` snapshot rows: ``{"kind": "snapshot", "state": ...}`` →
  the unwrapped full user state.
- Old ``DiffStorage`` delta rows: ``{"kind": "delta", "patch": ...}`` →
  ``{"set": patch, "del": []}``. Old deltas have no tombstones, which is
  correct — they never recorded removals.

``downgrade()`` rebuilds ``records`` from ``eventic_log`` honestly: a rebuild
is possible but cannot restore the phantom fields, and should not pretend to.
"""

import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0300_triad"
down_revision: Union[str, None] = "fold_properties_into_data"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MANAGED = frozenset({"id", "version", "version_id", "created_ts"})
PHANTOM = frozenset({"seam", "provides", "requires", "priority", "mode"})
_STRIP = MANAGED | PHANTOM


def _load(value):
    return json.loads(value) if isinstance(value, str) else value


def _strip(state: dict) -> dict:
    return {k: v for k, v in state.items() if k not in _STRIP}


def _unwrap(raw) -> tuple[dict, bool]:
    """(data, snapshot) in the 0.3 shapes, for one old row's ``data``."""
    data = _load(raw)
    if isinstance(data, dict) and data.get("kind") in ("snapshot", "delta"):
        if data["kind"] == "snapshot":
            return _strip(data.get("state", {})), True
        patch = _strip(data.get("patch", {}))
        return {"set": patch, "del": []}, False
    return _strip(data), True  # old FullSnapshot rows are full user state


def _data_expr(bind) -> str:
    """SQL for a JSON literal parameter. PG needs a jsonb cast; SQLite has no
    JSON type, so ``json()`` validates and stores the text (a bare
    ``CAST(... AS JSON)`` would NUMERIC-coerce the text to 0 on SQLite)."""
    if bind.dialect.name == "postgresql":
        return "CAST(:data AS JSONB)"
    return "json(:data)"


def upgrade() -> None:
    op.create_table(
        "eventic_log",
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("stream", sa.String(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("snapshot", sa.Boolean(), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.PrimaryKeyConstraint("version_id"),
        sa.UniqueConstraint("id", "version", name="uq_eventic_log_id_version"),
    )
    op.create_index(
        "ix_eventic_log_stream_id_version", "eventic_log", ["stream", "id", "version"]
    )
    op.create_index(
        "ix_eventic_log_snapshot", "eventic_log", ["stream", "id", "version"],
        postgresql_where=sa.text("snapshot"), sqlite_where=sa.text("snapshot"),
    )
    op.create_table(
        "eventic_head",
        sa.Column("stream", sa.String(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.PrimaryKeyConstraint("stream", "id"),
    )
    op.create_table(
        "eventic_outbox",
        sa.Column("seq", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("stream", sa.String(), nullable=False),
        sa.Column("record_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("delta", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True),
        sa.Column("handler_id", sa.String(), nullable=False),
        sa.Column("queue", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("seq"),
        sa.UniqueConstraint("version_id", "handler_id", name="uq_eventic_outbox_once"),
    )
    op.create_index("ix_eventic_outbox_ready", "eventic_outbox", ["available_at"])

    bind = op.get_bind()

    # -- copy + unwrap records -> eventic_log --------------------------- #
    old = bind.execute(
        sa.text(
            "SELECT version_id, id, version, class_type, created_ts, data "
            "FROM records ORDER BY class_type, id, version"
        )
    ).mappings()
    rows = []
    for r in old:
        data, snapshot = _unwrap(r["data"])
        rows.append(
            {
                "version_id": r["version_id"],
                "stream": r["class_type"],
                "id": r["id"],
                "version": r["version"],
                "kind": "create" if r["version"] == 0 else "update",
                "snapshot": snapshot,
                "committed_at": r["created_ts"],
                "data": json.dumps(data),
            }
        )
    if rows:
        bind.execute(
            sa.text(
                "INSERT INTO eventic_log (version_id, stream, id, version, kind, "
                "snapshot, committed_at, data) VALUES "
                "(:version_id, :stream, :id, :version, :kind, :snapshot, "
                ":committed_at, "
                + _data_expr(bind)
                + ")"
            ),
            rows,
        )

    # -- build the head projection by folding each stream ---------------- #
    log = bind.execute(
        sa.text(
            "SELECT stream, id, version, version_id, committed_at, snapshot, data "
            "FROM eventic_log ORDER BY stream, id, version"
        )
    ).mappings()
    heads: dict[tuple, dict] = {}
    for r in log:
        data = _load(r["data"])
        key = (r["stream"], r["id"])
        if r["snapshot"]:
            state = dict(data)
        else:
            state = heads.get(key, {}).get("_state", {})
            state = {**state, **data.get("set", {})}
            for k in data.get("del", []):
                state.pop(k, None)
        heads[key] = {
            "stream": r["stream"],
            "id": r["id"],
            "version": r["version"],
            "version_id": r["version_id"],
            "committed_at": r["committed_at"],
            "state": json.dumps(state),
            "_state": state,
        }
    if heads:
        bind.execute(
            sa.text(
                "INSERT INTO eventic_head (stream, id, version, version_id, "
                "committed_at, state) VALUES (:stream, :id, :version, :version_id, "
                ":committed_at, "
                + _data_expr(bind).replace(":data", ":state")
                + ")"
            ),
            [h for h in heads.values()],
        )

    op.drop_table("records")


def downgrade() -> None:
    """Rebuild ``records`` from ``eventic_log`` — honest, best-effort: the
    phantom fields cannot be restored, and this does not pretend to."""
    op.create_table(
        "records",
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("class_type", sa.String(), nullable=False),
        sa.Column("created_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.PrimaryKeyConstraint("version_id"),
        sa.UniqueConstraint("id", "version", name="uq_records_id_version"),
    )
    op.create_index(op.f("ix_records_id"), "records", ["id"], unique=False)

    bind = op.get_bind()
    log = bind.execute(
        sa.text(
            "SELECT version_id, stream, id, version, committed_at, data "
            "FROM eventic_log ORDER BY stream, id, version"
        )
    ).mappings()
    out = []
    for r in log:
        state = dict(_load(r["data"]))
        state.update(
            {
                "id": str(r["id"]),
                "version": r["version"],
                "version_id": str(r["version_id"]),
            }
        )
        out.append(
            {
                "version_id": r["version_id"],
                "id": r["id"],
                "version": r["version"],
                "class_type": r["stream"],
                "created_ts": r["committed_at"],
                "data": json.dumps(state),
            }
        )
    if out:
        bind = op.get_bind()
        bind.execute(
            sa.text(
                "INSERT INTO records (version_id, id, version, class_type, "
                "created_ts, data) VALUES (:version_id, :id, :version, :class_type, "
                ":created_ts, "
                + _data_expr(bind)
                + ")"
            ),
            out,
        )

    op.drop_table("eventic_outbox")
    op.drop_table("eventic_head")
    op.drop_table("eventic_log")
