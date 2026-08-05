"""Phase 14: every README code block is an executed doctest."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
README = ROOT / "README.md"

_PREAMBLE = """
from pydantic import BaseModel
from eventic import App, Stream, Subscription, Outbox
from eventic.sql import SQLite

class Audit(BaseModel):
    action: str

audits = Stream(Audit, name="audits")

class TodoV2(BaseModel):
    text: str
    done: bool = False
    priority: str = "normal"

def reindex(commit):
    pass

DATABASE_URL = "sqlite:///readme-doctest.db"
"""


def _code_blocks() -> list[str]:
    text = README.read_text()
    blocks = re.findall(r"```python\n(.*?)```", text, re.DOTALL)
    assert blocks, "no python blocks found in README"
    return blocks


@pytest.mark.parametrize("index", range(len(_code_blocks())))
def test_readme_code_block_runs(index: int) -> None:
    block = _code_blocks()[index]
    if "Postgres" in block:
        # the §11 shape names Postgres; run the shape on SQLite locally
        block = block.replace(
            "from eventic.sql import Postgres",
            "from eventic.sql import SQLite as Postgres",
        )
        # the shape's batch uses b[audits]; make the app install it
        block = block.replace("streams=[todos],", "streams=[todos, audits],")
    namespace: dict = {}
    exec(compile(_PREAMBLE, "<preamble>", "exec"), namespace)  # noqa: S102
    exec(compile(block, "<readme>", "exec"), namespace)  # noqa: S102
    _cleanup(namespace)


def _cleanup(namespace: dict) -> None:
    runtime = namespace.get("ev")
    if runtime is not None and hasattr(runtime, "store"):
        store = runtime.store
        if hasattr(store, "close"):
            store.close()
