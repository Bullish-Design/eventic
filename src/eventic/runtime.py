"""The runtime: pure delegation from ``Collection`` calls to the store.

``Collection`` plans (Phase 5), issues one ``store.commit``, hydrates, and
dispatches inline handlers. ``Batch`` accumulates requests and issues one
``store.commit`` on exit; it exposes no reads.
"""

from __future__ import annotations

import json
from typing import Any, TypeVar, cast
from uuid import UUID, uuid4

from pydantic import BaseModel

from eventic.app import App
from eventic.dispatch import dispatch_inline
from eventic.envelopes import Commit, Page, Revision
from eventic.errors import NotFound, UsageError
from eventic.hydration import hydrate
from eventic.planning import (
    changed_keys,
    plan_change,
    plan_create,
    plan_replace,
    state_tree,
)
from eventic.protocols import Store, StoreAdmin
from eventic.stream import Stream
from eventic.wire import CommitRequest, CommitResult, StoredRevision

AnyT = TypeVar("AnyT", bound=BaseModel)


class Collection[AnyT: BaseModel]:
    """The only handle through which a stream is read or written."""

    def __init__(self, runtime: Runtime, stream: Stream[AnyT]) -> None:
        self._runtime = runtime
        self._stream = stream

    @property
    def _app(self) -> App:
        return self._runtime.app

    @property
    def _store(self) -> Store:
        return self._runtime.store

    # -- writes -------------------------------------------------------------

    def create(
        self,
        state: AnyT,
        *,
        id: UUID | None = None,
        meta: object | None = None,
    ) -> Revision[AnyT, Any]:
        aggregate_id = id or uuid4()
        request = plan_create(self._app, self._stream, state, aggregate_id, meta)
        return self._commit_one(request, None)

    def change(
        self, base: Revision[AnyT, Any], /, **fields: object
    ) -> Revision[AnyT, Any]:
        request = plan_change(self._app, self._stream, base, dict(fields))
        return self._commit_one(request, base.state)

    def replace(
        self,
        base: Revision[AnyT, Any],
        state: AnyT,
        *,
        meta: object | None = None,
    ) -> Revision[AnyT, Any]:
        request = plan_replace(self._app, self._stream, base, state, meta)
        return self._commit_one(request, base.state)

    # -- reads --------------------------------------------------------------

    def get(self, id: UUID, *, revision: int | None = None) -> Revision[AnyT, Any]:
        from eventic.ids import AggregateKey

        key = AggregateKey(self._stream.name, id)
        stored = (
            self._store.head(key)
            if revision is None
            else self._store.revision(key, revision)
        )
        if stored is None:
            raise NotFound(
                "aggregate or exact revision absent",
                stream=self._stream.name,
                aggregate_id=id,
                revision=revision,
            )
        return hydrate(self._stream, self._app.meta, stored)

    def history(
        self, id: UUID, *, after: int = -1, limit: int = 100
    ) -> Page[Revision[AnyT, Any]]:
        from eventic.ids import AggregateKey

        if limit < 1:
            raise UsageError("limit must be >= 1")
        page = self._store.history(
            AggregateKey(self._stream.name, id), after=after, limit=limit
        )
        items = tuple(
            hydrate(self._stream, self._app.meta, stored) for stored in page.items
        )
        return Page[Revision[AnyT, Any]](items=items, cursor=page.cursor)

    def where(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
        **filters: object,
    ) -> Page[Revision[AnyT, Any]]:
        if limit < 1:
            raise UsageError("limit must be >= 1")
        page = self._store.search(
            self._stream.name,
            {k: v for k, v in filters.items()},  # type: ignore[misc]
            cursor=cursor,
            limit=limit,
        )
        items = tuple(
            hydrate(self._stream, self._app.meta, stored) for stored in page.items
        )
        return Page[Revision[AnyT, Any]](items=items, cursor=page.cursor)

    # -- plumbing -----------------------------------------------------------

    def _changed(self, request: CommitRequest, before: AnyT | None) -> frozenset[str]:
        after = json.loads(request.payload)
        if before is None:
            return frozenset(after)
        return changed_keys(state_tree(self._stream, before), after)

    def _commit_one(
        self, request: CommitRequest, before: AnyT | None
    ) -> Revision[AnyT, Any]:
        results = self._store.commit([request])
        return self._materialize(request, results[0], before)

    def _materialize(
        self,
        request: CommitRequest,
        result: CommitResult,
        before: AnyT | None,
    ) -> Revision[AnyT, Any]:
        stored = StoredRevision(
            stream=request.stream,
            aggregate_id=request.aggregate_id,
            revision=result.revision,
            revision_id=result.revision_id,
            kind=request.kind,
            schema_version=request.schema_version,
            meta_version=request.meta_version,
            encoding="",
            payload=json.loads(request.payload),
            digest=request.digest,
            meta=json.loads(request.meta),
            committed_at=result.committed_at,
        )
        revision = hydrate(self._stream, self._app.meta, stored)
        changed = self._changed(request, before)
        commit = Commit[AnyT, Any](
            kind=request.kind,
            revision=revision,
            changed=changed,
        )
        dispatch_inline(self._app, self._stream, commit)
        return revision


class Batch:
    """Accumulate writes; one ``store.commit`` on exit, then inline dispatch."""

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime
        self._entries: list[tuple[Stream[Any], CommitRequest, object]] = []

    def __getitem__[T: BaseModel](self, stream: Stream[T]) -> BatchCollection[T]:
        self._require_stream(stream)
        return BatchCollection(self, stream)

    def _require_stream(self, stream: Stream[Any]) -> None:
        if not any(s.name == stream.name for s in self._runtime.app.streams):
            raise UsageError(f"stream {stream.name} is not installed in this app")

    def _add(self, stream: Stream[Any], request: CommitRequest, before: object) -> None:
        self._entries.append((stream, request, before))

    def __enter__(self) -> Batch:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        if exc_type is not None:
            return False  # the block failed: nothing is sent to the store
        self._commit_all()
        return False

    def _commit_all(self) -> None:
        store = self._runtime.store
        requests = [request for _, request, _ in self._entries]
        results = store.commit(requests)
        for (stream, request, before), result in zip(
            self._entries, results, strict=True
        ):
            collection = Collection(self._runtime, stream)
            collection._materialize(request, result, before)  # type: ignore[reportPrivateUsage]


class BatchCollection[AnyT: BaseModel]:
    """Batch write handle; exposes create / change / replace ONLY."""

    def __init__(self, batch: Batch, stream: Stream[AnyT]) -> None:
        self._batch = batch
        self._stream = stream

    @property
    def _app(self) -> App:
        return self._batch._runtime.app  # type: ignore[reportPrivateUsage]

    def create(
        self,
        state: AnyT,
        *,
        id: UUID | None = None,
        meta: object | None = None,
    ) -> None:
        aggregate_id = id or uuid4()
        request = plan_create(self._app, self._stream, state, aggregate_id, meta)
        self._batch._add(self._stream, request, None)  # type: ignore[reportPrivateUsage]

    def change(self, base: Revision[AnyT, Any], /, **fields: object) -> None:
        request = plan_change(self._app, self._stream, base, dict(fields))
        self._batch._add(self._stream, request, base.state)  # type: ignore[reportPrivateUsage]

    def replace(
        self,
        base: Revision[AnyT, Any],
        state: AnyT,
        *,
        meta: object | None = None,
    ) -> None:
        request = plan_replace(self._app, self._stream, base, state, meta)
        self._batch._add(self._stream, request, base.state)  # type: ignore[reportPrivateUsage]


class Runtime:
    """``app.bind(store)`` — the only object through which anything is read or
    written."""

    def __init__(self, app: App, store: Store) -> None:
        self._app = app
        self._store = store
        self._collections: dict[str, Collection[Any]] = {}

    @property
    def app(self) -> App:
        return self._app

    @property
    def store(self) -> Store:
        return self._store

    def __getitem__[T: BaseModel](self, stream: Stream[T]) -> Collection[T]:
        declared = next((s for s in self._app.streams if s.name == stream.name), None)
        if declared is None:
            raise UsageError(f"stream {stream.name} is not installed in this app")
        if stream.name not in self._collections:
            self._collections[stream.name] = Collection(self, declared)
        return cast(Collection[T], self._collections[stream.name])

    def batch(self) -> Batch:
        return Batch(self)

    def admin(self) -> StoreAdmin:
        """A ``StoreAdmin`` for the bound store, or a clear error."""
        provider = getattr(self.store, "admin", None)
        if provider is None:
            raise UsageError("this store does not provide admin operations")
        return provider()
