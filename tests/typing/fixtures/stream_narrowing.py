"""Typing fixture (run by tests/typing/test_fixtures.py under basedpyright).

Asserts the Phase 3 exit gate: ``Stream(Todo, ...)`` narrows to ``Stream[Todo]``
and ``Revision[Todo, M].state`` is ``Todo``.
"""

from __future__ import annotations

from typing import assert_type

from pydantic import BaseModel

from eventic.envelopes import Revision
from eventic.stream import Stream


class Todo(BaseModel):
    text: str
    done: bool = False


s = Stream(Todo, name="todos")
assert_type(s, Stream[Todo])


def takes_stream(stream: Stream[Todo]) -> None: ...


takes_stream(Stream(Todo, name="todos"))
takes_stream(s)


def state_of(rev: Revision[Todo, BaseModel]) -> Todo:
    return rev.state


def narrowed(rev: Revision[Todo, BaseModel]) -> None:
    assert_type(rev.state, Todo)
