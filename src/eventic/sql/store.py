"""SQL store: execute-glue plus the §4.3 commit algorithm.

``statements.py`` builds the SQL; this module executes it and translates driver
exceptions at the boundary.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid5

from sqlalchemy import create_engine, func, select
from sqlalchemy import event as sa_event
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import IntegrityError

from eventic.encodings import Encoding, get_encoding
from eventic.errors import (
    EncodingError,
    EventicError,
    NotFound,
    RevisionConflict,
    StoreError,
    UndecodableRevision,
    UsageError,
)
from eventic.ids import AggregateKey, revision_id
from eventic.jsonx import JsonObject, JsonValue, canonical_bytes
from eventic.protocols import Capabilities, Store, StoreAdmin
from eventic.sql import statements as st
from eventic.sql.dialect import POSTGRES_CAPABILITIES, SQLITE_CAPABILITIES, Dialect
from eventic.wire import (
    ClaimedIntent,
    CommitRequest,
    CommitResult,
    IntentRequest,
    Settlement,
    StoredRevision,
)


def _parse_db_datetime(value: Any) -> datetime:
    """Normalize a database clock reading to tz-aware UTC."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _json_loads(value: Any) -> JsonObject:
    if isinstance(value, dict):
        return cast(JsonObject, value)  # postgres already parsed
    return json.loads(value)


def _is_revision_race(exc: IntegrityError) -> bool:
    """Is this violation the ``(stream, aggregate_id, revision)`` backstop?

    Only the unique index (or its deterministic ``revision_id`` primary key)
    means a lost write race — every other constraint violation (bad stream
    name, empty intent queue, kind/revision mismatch) is a genuine caller
    bug and must stay ``StoreError``.
    """
    orig = exc.orig
    constraint = getattr(getattr(orig, "diag", None), "constraint_name", None)
    if constraint in ("uq_revision", "eventic_revision_pkey"):
        return True
    message = str(orig)
    return "UNIQUE constraint failed: eventic_revision" in message


def _now(conn: Connection) -> datetime:
    return _parse_db_datetime(conn.execute(select(func.now())).scalar())


class SQLite(Store):
    """The development/testing/single-process backend.

    ``SQLite(":memory:")`` or a path. ``encodings`` maps stream names to an
    :class:`~eventic.encodings.Encoding` (default: ``snapshot/1``).
    """

    def __init__(
        self,
        url_or_path: str,
        *,
        encodings: Mapping[str, Encoding] | None = None,
        create_tables: bool = True,
    ) -> None:
        if "://" not in url_or_path:
            url_or_path = f"sqlite:///{url_or_path}"
        self.dialect = Dialect(name="sqlite", capabilities=SQLITE_CAPABILITIES)
        self._encodings = dict(encodings or {})
        if ":memory:" in url_or_path:
            from sqlalchemy.pool import StaticPool

            self.engine = create_engine(
                url_or_path,
                poolclass=StaticPool,
                connect_args={"check_same_thread": False},
            )
        else:
            self.engine = create_engine(url_or_path)
        self._install_events()
        if create_tables:
            self._create_tables()

    def _install_events(self) -> None:
        @sa_event.listens_for(self.engine, "connect")
        def _set_isolation(dbapi_conn: Any, _record: Any) -> None:  # type: ignore[reportUnusedFunction]
            dbapi_conn.isolation_level = None  # manual BEGIN control
            # WAL lets readers and the single writer coexist; busy_timeout
            # converts transient lock contention into a short wait.
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.execute("PRAGMA wal_autocheckpoint=5000")
            cursor.close()

        @sa_event.listens_for(self.engine, "begin")
        def _begin_immediate(conn: Connection) -> None:  # type: ignore[reportUnusedFunction]
            conn.exec_driver_sql("BEGIN IMMEDIATE")

    def _create_tables(self) -> None:
        from eventic.sql.tables import metadata

        metadata.create_all(self.engine)

    @property
    def capabilities(self) -> Capabilities:
        return self.dialect.capabilities

    def close(self) -> None:
        """Release pooled connections. Idempotent; safe after use."""
        self.engine.dispose()

    def admin(self) -> StoreAdmin:
        """A :class:`StoreAdmin` for this backend (CLI operations)."""
        from eventic.sql.admin import SqlAdmin

        return SqlAdmin(self)

    # -- write path ---------------------------------------------------------

    def commit(self, requests: Sequence[CommitRequest]) -> Sequence[CommitResult]:
        if len(requests) > self.capabilities.max_batch:
            raise UsageError(
                f"batch of {len(requests)} exceeds store max_batch "
                f"{self.capabilities.max_batch}"
            )
        try:
            with self.engine.begin() as conn:
                now = _now(conn)
                results = [self._commit_one(conn, request, now) for request in requests]
        except EventicError:
            raise
        except IntegrityError as exc:
            # §4.3 step 1: the unique index on (stream, aggregate_id, revision)
            # is the backstop. On Postgres the CAS read locks the head row, but
            # a concurrent *create* of a brand-new aggregate has no row to lock,
            # so both writers pass the CAS and the loser lands here. A lost race
            # must surface as RevisionConflict, not as an opaque StoreError, or
            # the documented optimistic-retry loop does not retry.
            if not _is_revision_race(exc):
                raise StoreError("commit failed") from exc
            raise RevisionConflict(
                "concurrent write to the same revision",
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise StoreError("commit failed") from exc
        return results

    def _commit_one(
        self, conn: Connection, request: CommitRequest, now: datetime
    ) -> CommitResult:
        target = (
            0 if request.expected_revision is None else request.expected_revision + 1
        )
        rid = revision_id(request.stream, request.aggregate_id, target)

        head_row = (
            conn.execute(
                st.select_head(request.stream, request.aggregate_id, for_update=True)
            )
            .mappings()
            .first()
        )
        existing = (
            conn.execute(
                st.select_revision_row(request.stream, request.aggregate_id, target)
            )
            .mappings()
            .first()
        )

        if existing is not None:
            if self._is_identical(existing, request):
                # Replay: the row already exists. The head must never move
                # backwards — a superseded replay is a no-op on the head
                # (I2). Only write the head when it is missing (repair) or
                # behind the row we are replaying.
                if head_row is None or head_row["revision"] < existing["revision"]:
                    self._upsert_head_from_row(conn, existing, request, rid, now)
                return CommitResult(
                    stream=request.stream,
                    aggregate_id=request.aggregate_id,
                    revision=target,
                    revision_id=rid,
                    committed_at=now,
                    replayed=True,
                )
            raise RevisionConflict(
                "row exists with different content",
                stream=request.stream,
                aggregate_id=request.aggregate_id,
                revision=target,
            )

        if head_row is None:
            if request.expected_revision is not None:
                raise RevisionConflict(
                    "aggregate does not exist",
                    stream=request.stream,
                    aggregate_id=request.aggregate_id,
                    revision=target,
                )
        elif (
            request.expected_revision is None
            or head_row["revision"] != request.expected_revision
        ):
            raise RevisionConflict(
                f"expected revision {request.expected_revision}, "
                f"head is {head_row['revision']}",
                stream=request.stream,
                aggregate_id=request.aggregate_id,
                revision=target,
            )

        encoding = self._encoding_for(request.stream)
        base = _json_loads(head_row["state"]) if head_row is not None else None
        base_rev = head_row["revision"] if head_row is not None else None
        snapshot = base is None or encoding.is_checkpoint(target)
        physical = (
            encoding.encode(
                _json_loads(request.payload), base=base, base_revision=base_rev
            )
            if not snapshot
            else _json_loads(request.payload)
        )
        row_encoding = "snapshot/1" if snapshot else encoding.encoding_id

        conn.execute(
            st.insert_revision(
                self.dialect,
                {
                    "revision_id": rid,
                    "stream": request.stream,
                    "aggregate_id": request.aggregate_id,
                    "revision": target,
                    "kind": request.kind,
                    "schema_version": request.schema_version,
                    "meta_version": request.meta_version,
                    "encoding": row_encoding,
                    "payload": physical,
                    "digest": request.digest,
                    "meta": _json_loads(request.meta),
                    "committed_at": now,
                },
            )
        )

        doc = self._decode_log_revision(
            conn, request.stream, request.aggregate_id, target
        )
        if canonical_bytes(doc) != request.payload:
            raise EncodingError(
                "decoded document does not match the request digest",
                stream=request.stream,
                aggregate_id=request.aggregate_id,
                revision=target,
            )

        conn.execute(
            st.upsert_head(
                self.dialect,
                {
                    "stream": request.stream,
                    "aggregate_id": request.aggregate_id,
                    "revision": target,
                    "revision_id": rid,
                    "schema_version": request.schema_version,
                    "meta_version": request.meta_version,
                    "state": doc,
                    "digest": request.digest,
                    "meta": _json_loads(request.meta),
                    "committed_at": now,
                },
            )
        )

        for intent in request.intents:
            conn.execute(
                st.insert_intents(
                    self.dialect,
                    [
                        {
                            "intent_id": _intent_id(intent),
                            "subscription_id": intent.subscription_id,
                            "revision_id": intent.revision_id,
                            "queue": intent.queue,
                            "status": "pending",
                            "attempts": 0,
                            "available_at": now,
                            "leased_until": None,
                            "last_error": None,
                            "created_at": now,
                        }
                    ],
                )
            )

        conn.execute(
            st.upsert_fingerprint(
                self.dialect,
                {
                    "stream": request.stream,
                    "schema_version": request.schema_version,
                    "fingerprint": request.fingerprint,
                    "first_seen": now,
                },
            )
        )

        return CommitResult(
            stream=request.stream,
            aggregate_id=request.aggregate_id,
            revision=target,
            revision_id=rid,
            committed_at=now,
            replayed=False,
        )

    def _is_identical(self, row: RowMapping, request: CommitRequest) -> bool:
        return (
            row["kind"] == request.kind
            and row["schema_version"] == request.schema_version
            and row["meta_version"] == request.meta_version
            and row["digest"] == request.digest
            and _json_loads(row["meta"]) == _json_loads(request.meta)
        )

    def _upsert_head_from_row(
        self,
        conn: Connection,
        row: RowMapping,
        request: CommitRequest,
        rid: UUID,
        now: datetime,
    ) -> None:
        doc = self._decode_log_revision(
            conn, request.stream, request.aggregate_id, row["revision"]
        )
        conn.execute(
            st.upsert_head(
                self.dialect,
                {
                    "stream": request.stream,
                    "aggregate_id": request.aggregate_id,
                    "revision": row["revision"],
                    "revision_id": row["revision_id"],
                    "schema_version": row["schema_version"],
                    "meta_version": row["meta_version"],
                    "state": doc,
                    "digest": row["digest"],
                    "meta": _json_loads(row["meta"]),
                    "committed_at": row["committed_at"],
                },
            )
        )

    # -- read path ----------------------------------------------------------

    def head(self, key: AggregateKey) -> StoredRevision | None:
        try:
            with self.engine.connect() as conn:
                row = (
                    conn.execute(
                        st.select_head(key.stream, key.aggregate_id, for_update=False)
                    )
                    .mappings()
                    .first()
                )
        except EventicError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StoreError("head read failed") from exc
        if row is None:
            return None
        return self._head_row_to_stored(row)

    def revision(self, key: AggregateKey, revision: int) -> StoredRevision | None:
        if revision < 0:
            raise UsageError("revision must be >= 0")
        try:
            with self.engine.connect() as conn:
                configured = self._encodings.get(key.stream)
                if configured is not None and configured.encoding_id == "delta/1":
                    every = int(getattr(configured, "every", 20))
                    if revision == 0 or revision % every == 0:
                        start = revision
                    else:
                        start = max(0, revision - every)
                    window = (
                        conn.execute(
                            st.select_window(
                                key.stream, key.aggregate_id, start, revision
                            )
                        )
                        .mappings()
                        .all()
                    )
                    if not window or window[-1]["revision"] != revision:
                        return None
                    payload = self._decode_window(
                        key.stream, key.aggregate_id, revision, window
                    )
                    return self._log_row_to_stored(window[-1], payload)
                row = (
                    conn.execute(
                        st.select_revision_row(key.stream, key.aggregate_id, revision)
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    return None
                payload = self._decode_log_revision(
                    conn, key.stream, key.aggregate_id, revision
                )
        except EventicError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StoreError("revision read failed") from exc
        return self._log_row_to_stored(row, payload)

    def history(self, key: AggregateKey, *, after: int, limit: int) -> Any:
        if limit < 1:
            raise UsageError("limit must be >= 1")
        try:
            with self.engine.connect() as conn:
                rows = (
                    conn.execute(
                        st.select_history(
                            key.stream, key.aggregate_id, since=after, limit=limit
                        )
                    )
                    .mappings()
                    .all()
                )
                items: list[StoredRevision] = []
                for row in rows:
                    payload = self._decode_log_revision(
                        conn, key.stream, key.aggregate_id, row["revision"]
                    )
                    items.append(self._log_row_to_stored(row, payload))
        except EventicError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StoreError("history read failed") from exc
        from eventic.envelopes import Page

        cursor = str(rows[-1]["revision"]) if len(rows) == limit else None
        return Page[StoredRevision](items=tuple(items), cursor=cursor)

    def search(
        self,
        stream: str,
        filters: Mapping[str, JsonValue],
        *,
        cursor: str | None,
        limit: int,
    ) -> Any:
        if limit < 1:
            raise UsageError("limit must be >= 1")
        cursor_uuid = UUID(cursor) if cursor is not None else None
        try:
            with self.engine.connect() as conn:
                rows = (
                    conn.execute(
                        st.search_heads(
                            self.dialect,
                            stream,
                            dict(filters),
                            cursor=cursor_uuid,
                            limit=limit,
                        )
                    )
                    .mappings()
                    .all()
                )
        except EventicError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StoreError("search failed") from exc
        from eventic.envelopes import Page

        items = tuple(self._head_row_to_stored(row) for row in rows)
        cursor_out = str(rows[-1]["aggregate_id"]) if len(rows) == limit else None
        return Page[StoredRevision](items=items, cursor=cursor_out)

    # -- delivery -----------------------------------------------------------

    def claim(
        self, queue: str, *, limit: int, lease: timedelta
    ) -> Sequence[ClaimedIntent]:
        if limit < 1:
            raise UsageError("limit must be >= 1")
        try:
            with self.engine.begin() as conn:
                # Lease checks are wall-clock delivery semantics; SQLite's
                # CURRENT_TIMESTAMP is second-precision and would stall
                # sub-second leases. committed_at is still the DB clock.
                now = datetime.now(UTC)
                rows = (
                    conn.execute(self.dialect.claim_select(queue, now, limit))
                    .mappings()
                    .all()
                )
                if rows:
                    conn.execute(
                        st.claim_mark_leased(
                            self.dialect,
                            [row["intent_id"] for row in rows],
                            now + lease,
                        )
                    )
        except EventicError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StoreError("claim failed") from exc
        return [
            ClaimedIntent(
                intent_id=row["intent_id"],
                subscription_id=row["subscription_id"],
                revision_id=row["revision_id"],
                queue=row["queue"],
                attempts=row["attempts"] + 1,
                stream=row["stream"],
                aggregate_id=row["aggregate_id"],
                revision=row["revision"],
            )
            for row in rows
        ]

    def settle(self, settlements: Sequence[Settlement]) -> None:
        try:
            with self.engine.begin() as conn:
                for statement in st.settle_intents(self.dialect, settlements):
                    conn.execute(statement)
        except EventicError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StoreError("settle failed") from exc

    # -- internal helpers ----------------------------------------------------

    def _encoding_for(self, stream: str) -> Encoding:
        return self._encodings.get(stream, get_encoding("snapshot/1"))

    def _decode_log_revision(
        self, conn: Connection, stream: str, aggregate_id: UUID, revision: int
    ) -> JsonObject:
        configured = self._encodings.get(stream)
        if configured is not None and configured.encoding_id == "delta/1":
            every = int(getattr(configured, "every", 20))
            if revision == 0 or revision % every == 0:
                start = revision
            else:
                start = max(0, revision - every)
            window = (
                conn.execute(st.select_window(stream, aggregate_id, start, revision))
                .mappings()
                .all()
            )
            return self._decode_window(stream, aggregate_id, revision, window)
        row = (
            conn.execute(st.select_revision_row(stream, aggregate_id, revision))
            .mappings()
            .first()
        )
        if row is None:
            raise NotFound(
                "revision absent",
                stream=stream,
                aggregate_id=aggregate_id,
                revision=revision,
            )
        if row["encoding"] == "snapshot/1":
            return _json_loads(row["payload"])
        physical = _json_loads(row["payload"])
        every = int(cast(int, physical.get("every") or 20))
        window = (
            conn.execute(
                st.select_window(
                    stream, aggregate_id, max(0, revision - every), revision
                )
            )
            .mappings()
            .all()
        )
        return self._decode_window(stream, aggregate_id, revision, window)

    def _decode_window(
        self,
        stream: str,
        aggregate_id: UUID,
        revision: int,
        window: Sequence[Any],
    ) -> JsonObject:
        """Decode a window of log rows ending at ``revision``."""
        if not window or window[-1]["revision"] != revision:
            raise NotFound(
                "revision absent",
                stream=stream,
                aggregate_id=aggregate_id,
                revision=revision,
            )
        checkpoint = next(
            (r for r in reversed(window) if r["encoding"] == "snapshot/1"),
            None,
        )
        if checkpoint is None:
            raise UndecodableRevision(
                "delta window has no checkpoint",
                stream=stream,
                aggregate_id=aggregate_id,
                revision=revision,
            )
        doc = _json_loads(checkpoint["payload"])
        prev = checkpoint["revision"]
        for r in window:
            if r["revision"] <= checkpoint["revision"]:
                continue
            if r["encoding"] == "snapshot/1":
                doc = _json_loads(r["payload"])
                prev = r["revision"]
                continue
            delta_payload = _json_loads(r["payload"])
            if delta_payload.get("base") != prev:
                raise UndecodableRevision(
                    "broken delta base chain",
                    stream=stream,
                    aggregate_id=aggregate_id,
                    revision=r["revision"],
                )
            doc = self._delta_decode(delta_payload, doc)
            prev = r["revision"]
        return doc

    def _delta_decode(self, payload: JsonObject, base: JsonObject) -> JsonObject:
        from eventic.encodings.delta import Delta

        every = int(cast(int, payload.get("every") or 20))
        return Delta(every=every).decode(payload, base=base)

    def _head_row_to_stored(self, row: RowMapping) -> StoredRevision:
        return StoredRevision(
            stream=row["stream"],
            aggregate_id=row["aggregate_id"],
            revision=row["revision"],
            revision_id=row["revision_id"],
            kind=row.get("kind", "change"),
            schema_version=row["schema_version"],
            meta_version=row["meta_version"],
            encoding="",
            payload=_json_loads(row["state"]),
            digest=row["digest"],
            meta=_json_loads(row["meta"]),
            committed_at=_parse_db_datetime(row["committed_at"]),
        )

    def _log_row_to_stored(
        self, row: RowMapping, payload: JsonObject
    ) -> StoredRevision:
        return StoredRevision(
            stream=row["stream"],
            aggregate_id=row["aggregate_id"],
            revision=row["revision"],
            revision_id=row["revision_id"],
            kind=row["kind"],
            schema_version=row["schema_version"],
            meta_version=row["meta_version"],
            encoding=row["encoding"],
            payload=payload,
            digest=row["digest"],
            meta=_json_loads(row["meta"]),
            committed_at=_parse_db_datetime(row["committed_at"]),
        )


def _intent_id(intent: IntentRequest) -> UUID:
    from eventic.ids import NS as _NS

    return uuid5(_NS, f"intent:{intent.subscription_id}:{intent.revision_id}")


class Postgres(SQLite):
    """The production backend.

    ``encodings`` maps stream names to an :class:`~eventic.encodings.Encoding`
    (default ``snapshot/1``). Schema is created with ``create_all`` for tests
    and with ``eventic schema upgrade`` (Alembic) in production; ``alembic
    check`` guarantees the two cannot drift.
    """

    def __init__(
        self,
        url: str,
        *,
        encodings: Mapping[str, Encoding] | None = None,
        create_tables: bool = True,
    ) -> None:
        self.dialect = Dialect(name="postgresql", capabilities=POSTGRES_CAPABILITIES)
        self._encodings = dict(encodings or {})
        self.engine = create_engine(url)
        self._install_events()
        if create_tables:
            self._create_tables()

    def _install_events(self) -> None:
        pass  # Postgres uses its default isolation and row locking
