"""Pure hydration: ``StoredRevision -> Revision[T, M]``.

Upcast if the stored schema version is behind the declared one, validate, and
build the envelope. Encoding-agnostic by construction: the store hands up a
logical document.
"""

from __future__ import annotations

from typing import Any

from eventic.envelopes import Revision
from eventic.errors import UndecodableRevision
from eventic.evolution import upcast
from eventic.jsonx import JsonObject, canonical_bytes
from eventic.meta import Meta
from eventic.stream import Stream
from eventic.wire import StoredRevision


def hydrate(
    stream: Stream[Any], meta_decl: Meta[Any], stored: StoredRevision
) -> Revision[Any, Any]:
    """Reconstruct a ``Revision`` from a stored logical document."""
    tree = _upcast_tree(
        stored.payload,
        stored.schema_version,
        stream.schema_version,
        stream.upcasters,
        subject=f"state of stream {stored.stream}",
    )
    state = stream.adapter.validate_json(canonical_bytes(tree))

    meta_tree = _upcast_tree(
        stored.meta,
        stored.meta_version,
        meta_decl.version,
        meta_decl.upcasters,
        subject=f"meta of stream {stored.stream}",
    )
    meta = meta_decl.adapter.validate_json(canonical_bytes(meta_tree))
    return Revision[Any, Any](
        stream=stored.stream,
        id=stored.aggregate_id,
        revision=stored.revision,
        revision_id=stored.revision_id,
        state=state,
        meta=meta,
        committed_at=stored.committed_at,
        digest=stored.digest,
    )


def _upcast_tree(
    tree: JsonObject,
    from_version: int,
    to_version: int,
    upcasters: Any,
    *,
    subject: str,
) -> JsonObject:
    if from_version > to_version:
        # F16: a row written by a newer schema_version read by an older
        # declaration. There is no downgrade path and no silent drop — a v2
        # row read by a v1 process is undecodable, and every rolling deploy
        # produces this window for a few minutes. Naming both versions and the
        # subject tells the operator which declaration is behind.
        raise UndecodableRevision(
            f"{subject}: stored schema version {from_version} is newer than "
            f"the declared version {to_version}",
            stored_version=from_version,
            declared_version=to_version,
        )
    if from_version == to_version:
        return tree
    return upcast(tree, upcasters, from_version=from_version, to_version=to_version)
