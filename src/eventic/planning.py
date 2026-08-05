"""Pure commit planning: build ``CommitRequest`` values from caller intent.

No I/O. ``plan_*`` functions take a stream, an app, and a value, and return a
fully-formed request the store can commit blindly.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from eventic.app import App
from eventic.canonical import canonicalize
from eventic.envelopes import Revision
from eventic.ids import revision_id
from eventic.jsonx import JsonObject, digest
from eventic.meta import Meta
from eventic.stream import Stream
from eventic.subscription import Outbox
from eventic.wire import CommitRequest, IntentRequest, Kind


def _canonical_bytes(stream: Stream[Any], value: object) -> bytes:
    return canonicalize(stream.adapter, stream.exclude_map, value)


def _meta_bytes(meta_decl: Meta[Any], meta: object) -> bytes:
    return canonicalize(meta_decl.adapter, meta_decl.exclude_map, meta)


def plan_create(
    app: App,
    stream: Stream[Any],
    state: Any,
    aggregate_id: UUID,
    meta: object | None = None,
) -> CommitRequest:
    """Plan the creation of a new aggregate at revision 0."""
    payload = _canonical_bytes(stream, state)
    meta_decl = app.meta
    meta_value = meta if meta is not None else meta_decl.model()
    meta_payload = _meta_bytes(meta_decl, meta_value)
    rid = revision_id(stream.name, aggregate_id, 0)
    return CommitRequest(
        stream=stream.name,
        aggregate_id=aggregate_id,
        expected_revision=None,
        kind="create",
        schema_version=stream.schema_version,
        payload=payload,
        digest=digest(payload),
        meta=meta_payload,
        meta_version=meta_decl.version,
        fingerprint=stream.fingerprint,
        intents=intents_for(app, stream, "create", rid),
    )


def _plan_change(
    app: App,
    stream: Stream[Any],
    base: Revision[Any, Any],
    new_state: Any,
    meta: object | None,
) -> CommitRequest:
    payload = _canonical_bytes(stream, new_state)
    meta_decl = app.meta
    meta_value = meta if meta is not None else base.meta
    meta_payload = _meta_bytes(meta_decl, meta_value)
    new_revision = base.revision + 1
    rid = revision_id(stream.name, base.id, new_revision)
    return CommitRequest(
        stream=stream.name,
        aggregate_id=base.id,
        expected_revision=base.revision,
        kind="change",
        schema_version=stream.schema_version,
        payload=payload,
        digest=digest(payload),
        meta=meta_payload,
        meta_version=meta_decl.version,
        fingerprint=stream.fingerprint,
        intents=intents_for(app, stream, "change", rid),
    )


def plan_change(
    app: App,
    stream: Stream[Any],
    base: Revision[Any, Any],
    fields: dict[str, object],
    meta: object | None = None,
) -> CommitRequest:
    """Plan an append on top of ``base``: ``base.state ⊕ fields``, validated.

    Never ``model_copy(update=...)`` — that bypasses validation. The merged
    state is validated through the stream adapter.
    """
    merged = base.state.model_dump(mode="python") | fields
    new_state = stream.adapter.validate_python(merged)
    return _plan_change(app, stream, base, new_state, meta)


def plan_replace(
    app: App,
    stream: Stream[Any],
    base: Revision[Any, Any],
    state: Any,
    meta: object | None = None,
) -> CommitRequest:
    """Plan an append whose state is a whole value, validated."""
    new_state = stream.adapter.validate_python(state)
    return _plan_change(app, stream, base, new_state, meta)


def changed_keys(before: JsonObject | None, after: JsonObject) -> frozenset[str]:
    """Top-level keys whose canonical value differs; all keys on create."""
    if before is None:
        return frozenset(after)
    return frozenset(k for k in after if before.get(k) != after[k])


def state_tree(stream: Stream[Any], state: object) -> JsonObject:
    """The canonical JSON tree of a state value (for ``changed`` diffs)."""
    return json.loads(_canonical_bytes(stream, state))


def intents_for(
    app: App, stream: Stream[Any], kind: Kind, revision_id_: UUID
) -> tuple[IntentRequest, ...]:
    """Every outbox intent owed for a commit; inline subscriptions are free."""
    intents: list[IntentRequest] = []
    for sub in app.subscriptions:
        if sub.stream.name != stream.name or kind not in sub.kinds:
            continue
        if isinstance(sub.delivery, Outbox):
            intents.append(
                IntentRequest(
                    subscription_id=sub.id,
                    revision_id=revision_id_,
                    queue=sub.delivery.queue,
                )
            )
    return tuple(intents)
