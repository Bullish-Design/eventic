"""Definition-of-done items 1–2 and the constraint matrix: no eventic class in
any user model's MRO, managed metadata cannot be state input, and the database
constraints reject invalid rows."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy.exc as sqlalchemy_exc
from pydantic import BaseModel
from sqlalchemy import text

from eventic.app import App
from eventic.envelopes import Revision
from eventic.errors import StoreError
from eventic.ids import AggregateKey
from eventic.jsonx import canonical_bytes, digest
from eventic.sql.store import SQLite
from eventic.stream import Stream
from eventic.wire import CommitRequest

AID = uuid.UUID(int=1)


class Todo(BaseModel):
    text: str
    done: bool = False


def test_no_eventic_class_in_user_model_mro() -> None:
    from eventic.testing.factories import ZOO

    for member in ZOO:
        for cls in member.model.__mro__[1:]:  # skip the model itself
            assert not cls.__module__.startswith("eventic"), (
                f"{member.name} inherits {cls.__module__}.{cls.__name__}"
            )


def test_managed_metadata_cannot_be_state_input() -> None:
    """Identity and commit metadata live on the envelope; the state model is
    untouched by eventic, so none of it can be supplied as state."""
    managed = {"stream", "revision_id", "revision", "committed_at", "digest"}
    todos = Stream(Todo, name="todos")
    app = App(id="demo", streams=[todos])
    store = SQLite(":memory:")
    try:
        ev = app.bind(store)
        t = ev[todos].create(Todo(text="x"))
        assert isinstance(t, Revision)
        # the envelope owns the managed fields...
        for name in managed:
            assert hasattr(t, name)
        # ...and the state is exactly the user's declared fields
        assert set(type(t.state).model_fields) == {"text", "done"}
    finally:
        store.close()


def test_constraints_reject_invalid_rows() -> None:
    store = SQLite(":memory:")
    try:
        with (
            store.engine.begin() as conn,
            pytest.raises(
                (sqlalchemy_exc.IntegrityError, sqlalchemy_exc.OperationalError)
            ),
        ):
            conn.execute(
                text(
                    "INSERT INTO eventic_revision (revision_id, stream, "
                    "aggregate_id, revision, kind, schema_version, "
                    "meta_version, encoding, payload, digest, meta, "
                    "committed_at) VALUES ('1', 'todos', '1', -1, "
                    "'create', 1, 1, 'snapshot/1', '{}', 'd', '{}', "
                    "CURRENT_TIMESTAMP)"
                )
            )  # negative revision
        with (
            store.engine.begin() as conn,
            pytest.raises(
                (sqlalchemy_exc.IntegrityError, sqlalchemy_exc.OperationalError)
            ),
        ):
            conn.execute(
                text(
                    "INSERT INTO eventic_revision (revision_id, stream, "
                    "aggregate_id, revision, kind, schema_version, "
                    "meta_version, encoding, payload, digest, meta, "
                    "committed_at) VALUES ('2', 'todos', '1', 0, "
                    "'bogus', 1, 1, 'snapshot/1', '{}', 'd', '{}', "
                    "CURRENT_TIMESTAMP)"
                )
            )  # invalid kind
        with (
            store.engine.begin() as conn,
            pytest.raises(
                (sqlalchemy_exc.IntegrityError, sqlalchemy_exc.OperationalError)
            ),
        ):
            conn.execute(
                text(
                    "INSERT INTO eventic_revision (revision_id, stream, "
                    "aggregate_id, revision, kind, schema_version, "
                    "meta_version, encoding, payload, digest, meta, "
                    "committed_at) VALUES ('3', '', '1', 0, "
                    "'create', 1, 1, 'snapshot/1', '{}', 'd', '{}', "
                    "CURRENT_TIMESTAMP)"
                )
            )  # empty stream
    finally:
        store.close()


def test_durable_kind_is_validated_by_store() -> None:
    """A request with a bogus kind is rejected at commit time."""
    store = SQLite(":memory:")
    try:
        payload = canonical_bytes({"text": "a", "done": False})
        from typing import cast

        request = CommitRequest(
            stream="todos",
            aggregate_id=AID,
            expected_revision=None,
            kind=cast(str, "bogus"),
            schema_version=1,
            payload=payload,
            digest=digest(payload),
            meta=canonical_bytes({}),
            meta_version=1,
            fingerprint="f",
        )
        with pytest.raises(StoreError):
            store.commit([request])
        assert store.head(AggregateKey("todos", AID)) is None
    finally:
        store.close()
