"""``StoreAdmin``: migrate, check, rebuild heads, verify. CLI-only, sync forever.

Rebuild and verify reconstruct heads from the log byte-exactly; the digest
column is what makes the comparison exact. Orphan heads (heads with no log
backing) cannot survive a rebuild because the scope is deleted first.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, tuple_

from eventic.app import App
from eventic.errors import StoreError
from eventic.protocols import RebuildReport, SchemaReport, StoreAdmin, VerifyReport
from eventic.sql import statements as st
from eventic.sql.store import (
    SQLite,
    _parse_db_datetime,  # type: ignore[reportPrivateUsage]
)


def _loads(value: Any) -> Any:
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    return json.loads(value)


def _decode_intent_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Opaque ``list_intents`` cursor -> ``(created_at, intent_id)``."""
    raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    created_at_iso, intent_id = json.loads(raw)
    return datetime.fromisoformat(created_at_iso), UUID(intent_id)


def _encode_intent_cursor(created_at: datetime, intent_id: UUID) -> str:
    payload = json.dumps([created_at.isoformat(), str(intent_id)])
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _accumulate_into(doc: dict[str, Any], row: Any) -> None:
    """Fold one log row into an in-flight per-aggregate document."""
    if row["encoding"] == "snapshot/1":
        doc.clear()
        doc.update(_loads(row["payload"]))
        return
    delta = _loads(row["payload"])
    from eventic.encodings.delta import Delta

    try:
        decoded = Delta(every=int(delta.get("every") or 20)).decode(delta, base=doc)
    except ValueError:
        return  # broken chain; the digest check reports it
    doc.clear()
    doc.update(decoded)


def _stream_log(
    conn: Any,
    store: SQLite,
    stream: str | None,
    chunk: int,
    *,
    emit: Any,
) -> None:
    """Fold the whole log in ``(stream, aggregate_id, revision)`` order,
    calling ``emit(key, doc)`` once per completed aggregate.

    The log is already ordered so that one aggregate's rows are contiguous;
    a document is finalised and released the moment the aggregate key
    changes. Peak memory is one in-flight document plus one chunk of rows —
    independent of the number of aggregates (F5). ``emit`` is a callback,
    not a generator: nothing lazy crosses any boundary.
    """
    current: tuple[str, UUID] | None = None
    doc: dict[str, Any] = {}
    after: Any = None
    while True:
        rows = (
            conn.execute(st.select_all_log_for(stream, chunk=chunk, after=after))
            .mappings()
            .all()
        )
        if not rows:
            break
        after = (rows[-1]["stream"], rows[-1]["aggregate_id"], rows[-1]["revision"])
        for row in rows:
            key = (row["stream"], row["aggregate_id"])
            if current is None:
                current = key
            if key != current:
                emit(current, doc)
                current = key
                doc = {}
            _accumulate_into(doc, row)
    if current is not None:
        emit(current, doc)


def _last_row(conn: Any, store: SQLite, key: tuple[str, UUID]) -> Any:
    from eventic.sql import statements as stmts

    return conn.execute(stmts.select_latest_revision(key[0], key[1])).mappings().first()


class SqlAdmin(StoreAdmin):
    """Admin operations on top of a ``SQLite`` or ``Postgres`` store."""

    def __init__(self, store: SQLite) -> None:
        self._store = store

    def migrate(self) -> None:
        try:
            from alembic import command
            from alembic.config import Config
        except ImportError as exc:  # pragma: no cover - migrate extra
            raise StoreError(
                "alembic is required for migrations; install eventic[migrate]"
            ) from exc
        import importlib.resources as resources

        migrations_pkg = resources.files("eventic.sql.migrations")
        cfg = Config(str(migrations_pkg / "alembic.ini"))
        cfg.set_main_option("sqlalchemy.url", str(self._store.engine.url))
        command.upgrade(cfg, "head")

    def check(self, app: App) -> SchemaReport:
        """Compare declared fingerprints to the ledger. Read-only (F12).

        A stream with no recorded baseline reports ``stored=None`` /
        ``ok=None`` — a third state, distinct from clean and from drift —
        because a first check on a never-written database must not *define*
        the baseline it is supposed to verify. Drift is only detected once a
        baseline exists.
        """
        engine = self._store.engine
        rows: list[tuple[str, int, str, str | None, bool | None]] = []
        drift = False
        baseline_missing = False
        with engine.connect() as conn:
            for stream in app.streams:
                declared = stream.fingerprint
                stored_row = (
                    conn.execute(
                        st.select_fingerprint(stream.name, stream.schema_version)
                    )
                    .mappings()
                    .first()
                )
                if stored_row is None:
                    baseline_missing = True
                    rows.append(
                        (stream.name, stream.schema_version, declared, None, None)
                    )
                    continue
                stored = stored_row["fingerprint"]
                ok = stored == declared
                drift = drift or not ok
                rows.append((stream.name, stream.schema_version, declared, stored, ok))
        return SchemaReport(
            streams=tuple(rows), drift=drift, baseline_missing=baseline_missing
        )

    def rebuild_heads(self, stream: str | None, *, chunk: int) -> RebuildReport:
        engine = self._store.engine
        mismatches = 0
        rebuilt = 0
        streams: set[str] = set()
        emitted_keys: set[tuple[str, UUID]] = set()
        with engine.begin() as conn:
            # Stream the existing heads: the orphan check needs only the key
            # tuples, so do not materialize the full row objects (F5: peak is
            # bounded by chunk + one aggregate's bookkeeping, not by the head
            # count).
            head_keys: set[tuple[str, UUID]] = set()
            for row in conn.execute(st.select_all_heads_for(stream)).mappings():
                head_keys.add((row["stream"], row["aggregate_id"]))
            conn.execute(st.delete_heads(stream))

            def emit(key: tuple[str, UUID], doc: dict[str, Any]) -> None:
                nonlocal mismatches, rebuilt
                emitted_keys.add(key)
                streams.add(key[0])
                from eventic.jsonx import canonical_bytes, digest

                last = _last_row(conn, self._store, key)
                if last is None or digest(canonical_bytes(doc)) != last["digest"]:
                    mismatches += 1
                    return
                conn.execute(
                    st.upsert_head(
                        self._store.dialect,
                        {
                            "stream": key[0],
                            "aggregate_id": key[1],
                            "revision": last["revision"],
                            "revision_id": last["revision_id"],
                            "schema_version": last["schema_version"],
                            "meta_version": last["meta_version"],
                            "state": doc,
                            "digest": last["digest"],
                            "meta": _loads(last["meta"]),
                            "committed_at": last["committed_at"],
                        },
                    )
                )
                rebuilt += 1

            _stream_log(conn, self._store, stream, chunk, emit=emit)
            orphans = len(head_keys - emitted_keys)
        return RebuildReport(
            streams=tuple(sorted(streams)),
            rebuilt=rebuilt,
            orphans_removed=orphans,
            mismatches=mismatches,
        )

    def verify(self, stream: str | None, *, chunk: int) -> VerifyReport:
        engine = self._store.engine
        checked = 0
        mismatches = 0
        streams: set[str] = set()
        with engine.connect() as conn:
            after: Any = None
            while True:
                rows = (
                    conn.execute(
                        st.select_all_log_for(stream, chunk=chunk, after=after)
                    )
                    .mappings()
                    .all()
                )
                if not rows:
                    break
                after = (
                    rows[-1]["stream"],
                    rows[-1]["aggregate_id"],
                    rows[-1]["revision"],
                )
                for row in rows:
                    streams.add(row["stream"])
                    checked += 1
                    doc = _decode_row(conn, self._store, row)
                    if doc is None:
                        mismatches += 1
                        continue
                    from eventic.jsonx import canonical_bytes, digest

                    if digest(canonical_bytes(doc)) != row["digest"]:
                        mismatches += 1

            def emit(key: tuple[str, UUID], doc: dict[str, Any]) -> None:
                nonlocal mismatches
                from eventic.jsonx import canonical_bytes, digest

                rebuilt_digest = digest(canonical_bytes(doc))
                live = (
                    conn.execute(st.select_head(key[0], key[1], for_update=False))
                    .mappings()
                    .first()
                )
                if live is None or live["digest"] != rebuilt_digest:
                    mismatches += 1

            _stream_log(conn, self._store, stream, chunk, emit=emit)
        return VerifyReport(
            streams=tuple(sorted(streams)),
            revisions_checked=checked,
            mismatches=mismatches,
        )

    def list_intents(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Paged listing of delivery intents.

        Ordered by ``(created_at, intent_id)`` — the immutable intent id keeps
        the keyset stable across pages even when several intents share a
        timestamp (R12). The returned cursor is opaque; pass it back to page
        on. ``limit`` bounds the page; without it the whole table is returned
        for backwards-compatible callers.
        """
        from eventic.sql.tables import eventic_intent as intents_t

        stmt = select(intents_t).order_by(intents_t.c.created_at, intents_t.c.intent_id)
        if status is not None:
            stmt = stmt.where(intents_t.c.status == status)
        if cursor is not None:
            created_at, intent_id = _decode_intent_cursor(cursor)
            stmt = stmt.where(
                tuple_(intents_t.c.created_at, intents_t.c.intent_id)
                > (created_at, intent_id)
            )
        if limit is not None:
            stmt = stmt.limit(limit)
        with self._store.engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        out = [dict(row) for row in rows]
        next_cursor: str | None = None
        if limit is not None and len(out) == limit:
            last = out[-1]
            next_cursor = _encode_intent_cursor(
                _parse_db_datetime(last["created_at"]), last["intent_id"]
            )
        return out, next_cursor

    def redrive(self, subscription_id: str) -> int:
        """Move dead intents of one subscription back to pending."""
        from datetime import datetime

        from sqlalchemy import update

        from eventic.sql.tables import eventic_intent as intents_t

        now = datetime.now(UTC)
        with self._store.engine.begin() as conn:
            result = conn.execute(
                update(intents_t)
                .where(
                    intents_t.c.subscription_id == subscription_id,
                    intents_t.c.status == "dead",
                )
                .values(
                    status="pending",
                    attempts=0,
                    available_at=now,
                    leased_until=None,
                    last_error=None,
                )
            )
        return result.rowcount or 0


def _decode_row(conn: Any, store: SQLite, row: Any) -> dict[str, Any] | None:
    if row["encoding"] == "snapshot/1":
        return _loads(row["payload"])
    delta = _loads(row["payload"])
    every = int(delta.get("every") or 20)
    window = (
        conn.execute(
            st.select_window(
                row["stream"],
                row["aggregate_id"],
                max(0, row["revision"] - every),
                row["revision"],
            )
        )
        .mappings()
        .all()
    )
    checkpoint = next(
        (r for r in reversed(window) if r["encoding"] == "snapshot/1"), None
    )
    if checkpoint is None:
        return None
    doc: dict[str, Any] = _loads(checkpoint["payload"])
    prev = checkpoint["revision"]
    from eventic.encodings.delta import Delta

    for r in window:
        if r["revision"] <= checkpoint["revision"]:
            continue
        if r["encoding"] == "snapshot/1":
            doc = _loads(r["payload"])
            prev = r["revision"]
            continue
        payload = _loads(r["payload"])
        if payload.get("base") != prev:
            return None
        doc = Delta(every=int(payload.get("every") or 20)).decode(payload, base=doc)
        prev = r["revision"]
    return doc
