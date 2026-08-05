"""Paging: history and where cursors page through bounded results."""

from __future__ import annotations

import uuid

from pydantic import BaseModel

from eventic.app import App
from eventic.runtime import Runtime
from eventic.sql.store import SQLite
from eventic.stream import Stream


class Todo(BaseModel):
    text: str
    done: bool = False


def _app() -> tuple[SQLite, Runtime, object]:
    store = SQLite(":memory:")
    todos = Stream(Todo, name="todos")
    runtime: Runtime = App(id="demo", streams=[todos]).bind(store)
    return store, runtime, todos


def test_history_paging_follows_cursor() -> None:
    store, runtime, todos = _app()
    try:
        t = runtime[todos].create(Todo(text="a"))
        for i in range(10):
            t = runtime[todos].change(t, text=f"t{i}")
        seen: list[int] = []
        cursor: str | None = None
        while True:
            page = runtime[todos].history(
                t.id, after=-1 if cursor is None else int(cursor), limit=4
            )
            seen.extend(r.revision for r in page.items)
            cursor = page.cursor
            if cursor is None:
                break
            if len(seen) > 20:  # safety valve
                break
        assert seen == list(range(11))
    finally:
        store.close()


def test_where_paging_follows_cursor() -> None:
    store, runtime, todos = _app()
    try:
        for i in range(15):
            runtime[todos].create(Todo(text=f"item-{i}", done=i % 2 == 0))
        ids: list[uuid.UUID] = []
        cursor: str | None = None
        while True:
            page = runtime[todos].where(done=True, limit=5, cursor=cursor)
            ids.extend(r.id for r in page.items)
            cursor = page.cursor
            if cursor is None:
                break
        assert len(ids) == 8  # 15 items, 8 with done=True (0,2,4,6,8,10,12,14)
        assert len(set(ids)) == 8
    finally:
        store.close()
