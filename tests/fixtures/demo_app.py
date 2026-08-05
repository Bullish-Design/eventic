"""A demo app for CLI end-to-end tests. Loaded by ``--app`` in fresh processes."""

from __future__ import annotations

from pydantic import BaseModel

from eventic import App
from eventic.stream import Stream


class Todo(BaseModel):
    text: str
    done: bool = False


todos = Stream(Todo, name="todos")

app = App(id="demo-cli", streams=[todos])
