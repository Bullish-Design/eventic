"""Phase 7: the SQLite store passes the whole store conformance suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from eventic.errors import EncodingError, StoreError
from eventic.ids import AggregateKey
from eventic.jsonx import canonical_bytes, digest
from eventic.sql.store import SQLite
from eventic.testing.runner import run_all, summary
from eventic.wire import CommitRequest


def make_sqlite(tmp_path: Path) -> SQLite:
    return SQLite(str(tmp_path / "store.db"))


@pytest.fixture()
def store(tmp_path: Path) -> SQLite:
    s = make_sqlite(tmp_path)
    yield s
    s.close()


def _scenario_stores(tmp_path: Path) -> tuple[list[SQLite], object]:
    import uuid as _uuid

    stores: list[SQLite] = []

    def factory() -> SQLite:
        store = SQLite(str(tmp_path / f"scenario-{_uuid.uuid4().hex}.db"))
        stores.append(store)
        return store

    return stores, factory


def test_conformance_suite_green_on_sqlite(tmp_path: Path) -> None:
    stores, factory = _scenario_stores(tmp_path)
    try:
        results = run_all(factory)
    finally:
        for s in stores:
            s.close()
    failed = [r for r in results if not r.passed and not r.skipped]
    assert not failed, summary(results)


def test_conformance_reports_skips_by_capability(tmp_path: Path) -> None:
    stores, factory = _scenario_stores(tmp_path)
    try:
        results = run_all(factory)
    finally:
        for s in stores:
            s.close()
    skipped = [r for r in results if r.skipped]
    reasons = {r.skipped_reason for r in skipped}
    assert reasons == {None} or all("concurrent_drainers" in (r or "") for r in reasons)


def test_head_derivation_asserts_digest(tmp_path: Path) -> None:
    """Breaking the encoder makes the commit fail loudly and write nothing."""

    class BrokenDelta:
        """A delta encoder that drops a key: encode/decode disagree."""

        encoding_id = "delta/1"
        every = 20

        def is_checkpoint(self, revision: int) -> bool:
            return revision == 0 or revision % self.every == 0

        def encode(self, doc, *, base, base_revision):
            out = {k: v for k, v in doc.items() if k != "text"}
            return {"every": self.every, "base": base_revision, "set": out, "del": []}

        def decode(self, payload, *, base):
            result = dict(base or {})
            result.update(payload["set"])
            for key in payload["del"]:
                result.pop(key, None)
            return result

    store = SQLite(str(tmp_path / "delta.db"), encodings={"todos": BrokenDelta()})
    create = CommitRequest(
        stream="todos",
        aggregate_id=__import__("uuid").UUID(int=1),
        expected_revision=None,
        kind="create",
        schema_version=1,
        payload=canonical_bytes({"text": "a", "done": False}),
        digest=digest(canonical_bytes({"text": "a", "done": False})),
        meta=canonical_bytes({}),
        meta_version=1,
        fingerprint="f",
    )
    store.commit([create])
    change = CommitRequest(
        stream="todos",
        aggregate_id=__import__("uuid").UUID(int=1),
        expected_revision=0,
        kind="change",
        schema_version=1,
        payload=canonical_bytes({"text": "b", "done": False}),
        digest=digest(canonical_bytes({"text": "b", "done": False})),
        meta=canonical_bytes({}),
        meta_version=1,
        fingerprint="f",
    )
    with pytest.raises(EncodingError):
        store.commit([change])
    assert store.head(AggregateKey("todos", create.aggregate_id)).revision == 0
    store.close()


def test_statements_have_no_execute() -> None:
    import ast
    from pathlib import Path as P

    tree = ast.parse(P("src/eventic/sql/statements.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr == "execute":
                raise AssertionError("statements.py must not execute")


def test_sqlite_json_paths(store: SQLite) -> None:
    """Missing path and explicit null are distinct; dotted keys addressable."""
    from eventic.jsonx import canonical_bytes, digest
    from eventic.wire import CommitRequest

    def commit(aid: int, doc: dict, stream: str = "todos") -> None:
        payload = canonical_bytes(doc)
        store.commit(
            [
                CommitRequest(
                    stream=stream,
                    aggregate_id=__import__("uuid").UUID(int=aid),
                    expected_revision=None,
                    kind="create",
                    schema_version=1,
                    payload=payload,
                    digest=digest(payload),
                    meta=canonical_bytes({}),
                    meta_version=1,
                    fingerprint="f",
                )
            ]
        )

    commit(1, {"text": "a", "meta": None})
    commit(2, {"text": "b"})
    commit(3, {"text": "c", "meta": {"tag": "x"}})
    commit(4, {"text": "d", "meta.tag": {"literal": True}})

    r1 = store.search("todos", {"meta": None}, cursor=None, limit=100)
    assert [i.aggregate_id.int for i in r1.items] == [1]
    r2 = store.search("todos", {"text": "b", "meta": None}, cursor=None, limit=100)
    assert r2.items == ()
    r3 = store.search("todos", {"meta.tag": "x"}, cursor=None, limit=100)
    assert [i.aggregate_id.int for i in r3.items] == [3]
    r4 = store.search("todos", {"meta\\.tag.literal": True}, cursor=None, limit=100)
    assert [i.aggregate_id.int for i in r4.items] == [4]


def test_error_translation(store: SQLite) -> None:
    from eventic.jsonx import canonical_bytes, digest
    from eventic.wire import CommitRequest

    bad = CommitRequest(
        stream="",
        aggregate_id=__import__("uuid").UUID(int=1),
        expected_revision=None,
        kind="create",
        schema_version=1,
        payload=canonical_bytes({"a": 1}),
        digest=digest(canonical_bytes({"a": 1})),
        meta=canonical_bytes({}),
        meta_version=1,
        fingerprint="f",
    )
    with pytest.raises(StoreError):
        store.commit([bad])
