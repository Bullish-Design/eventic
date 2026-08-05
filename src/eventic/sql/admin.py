"""``StoreAdmin``: migrate, check, rebuild heads, verify. CLI-only, sync forever.

Rebuild and verify reconstruct heads from the log byte-exactly; the digest
column is what makes the comparison exact. Orphan heads (heads with no log
backing) cannot survive a rebuild because the scope is deleted first.
"""

from __future__ import annotations

import json
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select

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


def _accumulate(final: dict[tuple[str, UUID], dict[str, Any]], row: Any) -> None:
    """Fold one log row into the persistent per-aggregate document state."""
    key = (row["stream"], row["aggregate_id"])
    doc = final.setdefault(key, {})
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
    conn: Any, store: SQLite, stream: str | None, chunk: int
) -> dict[tuple[str, UUID], dict[str, Any]]:
    """Read the whole log in chunks, decoding incrementally across boundaries."""
    final: dict[tuple[str, UUID], dict[str, Any]] = {}
    offset = 0
    while True:
        rows = (
            conn.execute(st.select_all_log_for(stream, chunk=chunk, offset=offset))
            .mappings()
            .all()
        )
        if not rows:
            break
        offset += len(rows)
        for row in rows:
            _accumulate(final, row)
    return final


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
        engine = self._store.engine
        rows: list[tuple[str, int, str, str, bool]] = []
        drift = False
        with engine.begin() as conn:
            now = _parse_db_datetime(conn.execute(select(func.now())).scalar())
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
                    conn.execute(
                        st.upsert_fingerprint(
                            self._store.dialect,
                            {
                                "stream": stream.name,
                                "schema_version": stream.schema_version,
                                "fingerprint": declared,
                                "first_seen": now,
                            },
                        )
                    )
                    rows.append(
                        (stream.name, stream.schema_version, declared, declared, True)
                    )
                    continue
                stored = stored_row["fingerprint"]
                ok = stored == declared
                drift = drift or not ok
                rows.append((stream.name, stream.schema_version, declared, stored, ok))
        return SchemaReport(streams=tuple(rows), drift=drift)

    def rebuild_heads(self, stream: str | None, *, chunk: int) -> RebuildReport:
        engine = self._store.engine
        mismatches = 0
        rebuilt = 0
        streams: set[str] = set()
        with engine.begin() as conn:
            deleted = conn.execute(st.select_all_heads_for(stream)).mappings().all()
            head_keys = {(h["stream"], h["aggregate_id"]) for h in deleted}
            log = _stream_log(conn, self._store, stream, chunk)
            streams.update(key[0] for key in log)
            orphans = len(head_keys - set(log))
            conn.execute(st.delete_heads(stream))
            for key, doc in log.items():
                from eventic.jsonx import canonical_bytes, digest

                last = _last_row(conn, self._store, key)
                if last is None or digest(canonical_bytes(doc)) != last["digest"]:
                    mismatches += 1
                    continue
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
            offset = 0
            while True:
                rows = (
                    conn.execute(
                        st.select_all_log_for(stream, chunk=chunk, offset=offset)
                    )
                    .mappings()
                    .all()
                )
                if not rows:
                    break
                offset += len(rows)
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
            log = _stream_log(conn, self._store, stream, chunk)
            for key, doc in log.items():
                from eventic.jsonx import canonical_bytes, digest

                rebuilt_digest = digest(canonical_bytes(doc))
                live = (
                    conn.execute(st.select_head(key[0], key[1], for_update=False))
                    .mappings()
                    .first()
                )
                if live is None or live["digest"] != rebuilt_digest:
                    mismatches += 1
        return VerifyReport(
            streams=tuple(sorted(streams)),
            revisions_checked=checked,
            mismatches=mismatches,
        )


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
