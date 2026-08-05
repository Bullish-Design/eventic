"""Phase 12: encoding conformance — digest equality at every revision, under
both encodings, including delta's historical failure modes."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import event as sa_event

from eventic.encodings import get_encoding
from eventic.errors import UndecodableRevision
from eventic.ids import AggregateKey
from eventic.jsonx import canonical_bytes, digest
from eventic.sql.store import SQLite
from eventic.wire import CommitRequest

AID = uuid.UUID(int=1)


def _request(
    i: int, text: str, expected: int | None, schema_version: int = 1
) -> CommitRequest:
    payload = canonical_bytes({"text": text, "done": bool(i % 2), "n": i})
    return CommitRequest(
        stream="todos",
        aggregate_id=AID,
        expected_revision=expected,
        kind="create" if expected is None else "change",
        schema_version=schema_version,
        payload=payload,
        digest=digest(payload),
        meta=canonical_bytes({}),
        meta_version=1,
        fingerprint="f",
    )


def _write(store: SQLite, count: int) -> list[bytes]:
    payloads: list[bytes] = []
    for i in range(count):
        payload = canonical_bytes({"text": f"t{i}", "done": bool(i % 2), "n": i})
        store.commit(
            [
                CommitRequest(
                    stream="todos",
                    aggregate_id=AID,
                    expected_revision=None if i == 0 else i - 1,
                    kind="create" if i == 0 else "change",
                    schema_version=1,
                    payload=payload,
                    digest=digest(payload),
                    meta=canonical_bytes({}),
                    meta_version=1,
                    fingerprint="f",
                )
            ]
        )
        payloads.append(payload)
    return payloads


@pytest.mark.parametrize("encoding_id", ["snapshot/1", "delta/1"])
def test_digest_equality_at_every_revision(tmp_path: Path, encoding_id: str) -> None:
    encoding = get_encoding(encoding_id)
    store = SQLite(
        str(tmp_path / f"{encoding_id.replace('/', '-')}.db"),
        encodings={"todos": encoding},
    )
    payloads = _write(store, 30)
    for n, payload in enumerate(payloads):
        stored = store.revision(AggregateKey("todos", AID), n)
        assert stored is not None
        assert stored.digest == digest(payload)
        assert stored.payload == __import__("json").loads(payload)
    head = store.head(AggregateKey("todos", AID))
    assert head is not None
    assert head.digest == digest(payloads[-1])
    store.close()


def test_delta_checkpoint_rows_are_snapshots(tmp_path: Path) -> None:
    from eventic.encodings.delta import Delta

    store = SQLite(str(tmp_path / "ck.db"), encodings={"todos": Delta(every=3)})
    _write(store, 7)
    from sqlalchemy import text as _text

    with store.engine.connect() as conn:
        rows = conn.execute(
            _text("SELECT revision, encoding FROM eventic_revision ORDER BY revision")
        ).all()
    encodings = {rev: enc for rev, enc in rows}
    assert encodings[0] == "snapshot/1"
    assert encodings[3] == "snapshot/1"  # every=3: checkpoints at 0 and 3
    assert encodings[6] == "snapshot/1"
    assert encodings[1] == "delta/1"
    store.close()


def test_delta_field_removal_round_trips(tmp_path: Path) -> None:
    """The tombstone test: a removed field never resurrects on read."""
    store = SQLite(
        str(tmp_path / "tomb.db"), encodings={"todos": get_encoding("delta/1")}
    )
    # write with n present, then drop n
    payload0 = canonical_bytes({"text": "a", "done": False, "n": 1})
    store.commit(
        [
            CommitRequest(
                stream="todos",
                aggregate_id=AID,
                expected_revision=None,
                kind="create",
                schema_version=1,
                payload=payload0,
                digest=digest(payload0),
                meta=canonical_bytes({}),
                meta_version=1,
                fingerprint="f",
            )
        ]
    )
    payload1 = canonical_bytes({"text": "a", "done": False})
    store.commit(
        [
            CommitRequest(
                stream="todos",
                aggregate_id=AID,
                expected_revision=0,
                kind="change",
                schema_version=1,
                payload=payload1,
                digest=digest(payload1),
                meta=canonical_bytes({}),
                meta_version=1,
                fingerprint="f",
            )
        ]
    )
    rev1 = store.revision(AggregateKey("todos", AID), 1)
    assert rev1 is not None
    assert "n" not in rev1.payload  # the tombstone removed it
    # and reading revision 0 still has it
    rev0 = store.revision(AggregateKey("todos", AID), 0)
    assert rev0 is not None
    assert rev0.payload["n"] == 1
    store.close()


def test_delta_corrupted_row_raises_or_mismatch(tmp_path: Path) -> None:
    store = SQLite(
        str(tmp_path / "corrupt.db"), encodings={"todos": get_encoding("delta/1")}
    )
    _write(store, 5)
    import json as _json

    from sqlalchemy import text as _text

    # corrupt the CONTENT but keep the chain intact: decodes, digest differs
    corrupted = _json.dumps(
        {"every": 20, "base": 1, "set": {"text": "wrong"}, "del": []}
    )
    with store.engine.begin() as conn:
        conn.execute(
            _text("UPDATE eventic_revision SET payload = :p WHERE revision = 2"),
            {"p": corrupted},
        )
    stored = store.revision(AggregateKey("todos", AID), 2)
    assert stored is not None
    assert stored.digest != digest(canonical_bytes(stored.payload))
    # verify reports it
    report = store.admin().verify("todos", chunk=100)
    assert report.mismatches >= 1

    # a corrupted BASE chain is a loud read-time error
    broken = _json.dumps({"every": 20, "base": 0, "set": {"text": "wrong"}, "del": []})
    with store.engine.begin() as conn:
        conn.execute(
            _text("UPDATE eventic_revision SET payload = :p WHERE revision = 2"),
            {"p": broken},
        )
    with pytest.raises(UndecodableRevision):
        store.revision(AggregateKey("todos", AID), 2)
    store.close()


def test_delta_missing_checkpoint_raises(tmp_path: Path) -> None:
    store = SQLite(
        str(tmp_path / "no-ck.db"), encodings={"todos": get_encoding("delta/1")}
    )
    _write(store, 5)
    from sqlalchemy import text as _text

    with store.engine.begin() as conn:
        conn.execute(_text("DELETE FROM eventic_revision WHERE revision = 0"))
        conn.execute(_text("DELETE FROM eventic_head"))
    # revision 1's window starts at the missing checkpoint
    with pytest.raises(UndecodableRevision):
        store.revision(AggregateKey("todos", AID), 1)
    store.close()


def test_encoding_switch_mid_life_leaves_history_readable(tmp_path: Path) -> None:
    snapshot = SQLite(str(tmp_path / "switch.db"))
    payloads = _write(snapshot, 6)
    snapshot.close()
    # reopen the same database with delta configuration
    delta = SQLite(
        str(tmp_path / "switch.db"), encodings={"todos": get_encoding("delta/1")}
    )
    payload = canonical_bytes({"text": "delta-era", "done": False})
    delta.commit(
        [
            CommitRequest(
                stream="todos",
                aggregate_id=AID,
                expected_revision=5,
                kind="change",
                schema_version=1,
                payload=payload,
                digest=digest(payload),
                meta=canonical_bytes({}),
                meta_version=1,
                fingerprint="f",
            )
        ]
    )
    # every historical revision is readable
    for n, p in enumerate(payloads):
        stored = delta.revision(AggregateKey("todos", AID), n)
        assert stored is not None
        assert stored.digest == digest(p)
    latest = delta.revision(AggregateKey("todos", AID), 6)
    assert latest is not None
    assert latest.payload["text"] == "delta-era"
    delta.close()


def test_point_read_touches_bounded_rows(tmp_path: Path) -> None:
    """A point read at a high revision with K=20 touches at most 21 rows."""
    from eventic.encodings.delta import Delta

    store = SQLite(str(tmp_path / "bounded.db"), encodings={"todos": Delta(every=20)})
    _write(store, 50)

    counts: list[int] = []

    @sa_event.listens_for(store.engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        if statement.lstrip().upper().startswith("SELECT"):
            counts.append(1)

    store.revision(AggregateKey("todos", AID), 40)
    assert len(counts) == 1  # a checkpoint read is one query
    counts.clear()
    store.revision(AggregateKey("todos", AID), 41)
    # a delta read is one bounded window query [21..41]
    assert len(counts) == 1
    store.close()


def test_verify_clean_under_delta(tmp_path: Path) -> None:
    store = SQLite(
        str(tmp_path / "vdelta.db"), encodings={"todos": get_encoding("delta/1")}
    )
    _write(store, 25)
    report = store.admin().verify(None, chunk=4)
    assert report.mismatches == 0
    assert report.revisions_checked == 25
    store.close()
