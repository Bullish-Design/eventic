"""Phase 11: schema evolution — old rows readable at every read path."""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

import pytest
from pydantic import BaseModel

from eventic.app import App
from eventic.evolution import make_upcaster
from eventic.jsonx import canonical_bytes, digest
from eventic.runtime import Runtime
from eventic.sql.store import SQLite
from eventic.stream import Stream
from eventic.wire import CommitRequest


class TaskV1(BaseModel):
    text: str
    done: bool = False


class TaskV2(BaseModel):
    text: str
    done: bool = False
    priority: str = "normal"


def _v2_stream() -> Stream[TaskV2]:
    return Stream(
        TaskV2,
        name="tasks",
        schema_version=2,
        upcasters={1: make_upcaster(1, 2, lambda tree: {**tree, "priority": "normal"})},
    )


def _seed_v1(store: SQLite, count: int = 3) -> list[uuid.UUID]:
    ids = [uuid.UUID(int=i) for i in range(1, count + 1)]
    for i, aid in enumerate(ids):
        payload = canonical_bytes({"text": f"v1-{i}", "done": False})
        store.commit(
            [
                CommitRequest(
                    stream="tasks",
                    aggregate_id=aid,
                    expected_revision=None,
                    kind="create",
                    schema_version=1,
                    payload=payload,
                    digest=digest(payload),
                    meta=canonical_bytes({}),
                    meta_version=1,
                    fingerprint="v1-fingerprint",
                )
            ]
        )
    return ids


def _v2_app() -> App:
    return App(id="demo", streams=[_v2_stream()])


def _worker_app(seen: list) -> App:
    from eventic.envelopes import Commit
    from eventic.subscription import Outbox, Subscription

    def handler(commit: Commit[TaskV2, BaseModel]) -> None:
        seen.append(commit)

    return App(
        id="demo",
        streams=[_v2_stream()],
        subscriptions=[
            Subscription(
                id="o",
                stream=_v2_stream(),
                handler=handler,
                delivery=Outbox(queue="q"),
            )
        ],
    )


def test_v1_rows_read_through_v2_at_every_path(tmp_path: Path) -> None:
    store = SQLite(str(tmp_path / "evo.db"))
    ids = _seed_v1(store)
    runtime: Runtime = _v2_app().bind(store)
    todos = runtime.app.streams[0]

    # get
    rev = runtime[todos].get(ids[0])
    assert rev.state.priority == "normal"
    assert rev.state.text == "v1-0"
    # history
    page = runtime[todos].history(ids[0])
    assert page.items[0].state.priority == "normal"
    # where
    found = runtime[todos].where(done=False)
    assert len(found.items) == 3
    assert all(r.state.priority == "normal" for r in found.items)
    # worker reconstruction
    from eventic.worker import Worker

    deliveries: list = []
    app = _worker_app(deliveries)
    # stage an intent by writing through the v2 app against the same db
    runtime2 = app.bind(store)
    runtime2[app.streams[0]].change(runtime[todos].get(ids[0]), done=True)
    worker = Worker(app, store, queue="q")
    worker.drain_once()
    assert deliveries
    assert deliveries[0].revision.state.priority == "normal"
    store.close()


def test_v1_writer_and_v2_reader_concurrently(tmp_path: Path) -> None:
    store = SQLite(str(tmp_path / "rolling.db"))
    ids = _seed_v1(store)
    v1_app = App(id="writer", streams=[Stream(TaskV1, name="tasks", schema_version=1)])
    v1_app.bind(store)
    v2_runtime: Runtime = _v2_app().bind(store)

    stop = threading.Event()
    errors: list[Exception] = []

    def write_loop() -> None:
        try:
            i = 0
            while not stop.is_set():
                payload = canonical_bytes({"text": f"live-{i}", "done": bool(i % 2)})
                store.commit(
                    [
                        CommitRequest(
                            stream="tasks",
                            aggregate_id=ids[0],
                            expected_revision=i,
                            kind="change",
                            schema_version=1,
                            payload=payload,
                            digest=digest(payload),
                            meta=canonical_bytes({}),
                            meta_version=1,
                            fingerprint="v1-fingerprint",
                        )
                    ]
                )
                i += 1
                import time as _time

                _time.sleep(0.005)  # keep WAL checkpoints rare; not a stress test
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def read_loop() -> None:
        try:
            for _ in range(50):
                rev = v2_runtime[v2_runtime.app.streams[0]].get(ids[0])
                assert rev.state.priority == "normal"
                assert rev.state.text.startswith(("v1-", "live-"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=write_loop)
    t2 = threading.Thread(target=read_loop)
    t1.start()
    t2.start()
    t2.join(timeout=10)
    stop.set()
    t1.join(timeout=10)
    assert not errors, errors
    assert not t2.is_alive()
    store.close()


def test_fingerprint_drift_detected_by_admin_check(tmp_path: Path) -> None:
    store = SQLite(str(tmp_path / "fp.db"))
    _seed_v1(store)
    # declared schema_version=1 with the REAL fingerprint; the stored row was
    # seeded with the literal "v1-fingerprint", so they must differ
    app = App(id="demo", streams=[Stream(TaskV1, name="tasks", schema_version=1)])
    report = store.admin().check(app)
    assert report.drift
    assert report.streams[0][4] is False
    store.close()


def test_missing_upcaster_is_declaration_error() -> None:
    from eventic.errors import IncompleteUpcasterChain

    with pytest.raises(IncompleteUpcasterChain):
        Stream(TaskV2, name="tasks", schema_version=2, upcasters={})


def test_upcaster_signature_is_pure_json() -> None:
    """The Upcaster protocol passes only a JSON tree: a side-effecting
    upcaster is impossible to write without lying about its signature."""
    import inspect

    from eventic.evolution import Upcaster

    sig = inspect.signature(Upcaster.__call__)
    params = list(sig.parameters.values())
    assert len(params) == 2  # self + tree
    from eventic.jsonx import JsonObject

    assert (
        params[1].annotation is JsonObject or str(params[1].annotation) == "JsonObject"
    )


def test_fixture_corpus_sql_script_loads(tmp_path: Path) -> None:
    """A database produced by an earlier schema version reads forward."""
    fixture = Path(__file__).parent.parent / "fixtures" / "evolution" / "v1_tasks.sql"
    assert fixture.exists()
    db_path = tmp_path / "fixture.db"
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.executescript(fixture.read_text())
    conn.commit()
    conn.close()

    store = SQLite(str(db_path))
    runtime: Runtime = _v2_app().bind(store)
    todos = runtime.app.streams[0]
    rev = runtime[todos].get(uuid.UUID(int=7))
    assert rev.state.priority == "normal"
    assert rev.state.text == "from-fixture"
    store.close()
