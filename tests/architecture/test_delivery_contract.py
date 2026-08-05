"""Phase 10 gate (I9): the phrase "exactly once" appears nowhere, and the
"at-least-once" contract is stated in the delivery documentation."""

from __future__ import annotations

from pathlib import Path

import eventic

ROOT = Path(eventic.__file__).resolve().parent.parent.parent


def _source_files() -> list[Path]:
    """Every scanned documentation surface: src, docs, and root *.md.

    README.md lives at the repo root and is the only file whose code blocks
    run as doctests (F14). AGENTS.md / CLAUDE.md are a symlink pair; resolve()
    dedupes them so neither is read twice.
    """
    paths = (
        sorted(ROOT.joinpath("src").rglob("*.py"))
        + sorted(ROOT.joinpath("docs").rglob("*.md"))
        + sorted(ROOT.glob("*.md"))
    )
    return sorted({p.resolve() for p in paths})


def test_exactly_once_appears_nowhere() -> None:
    for path in _source_files():
        if "migrations" in path.parts:
            continue
        text = path.read_text()
        assert "exactly once" not in text.lower(), path


def test_at_least_once_stated() -> None:
    delivery_text = ""
    for path in _source_files():
        if path.name in ("worker.py", "CONCEPT.md", "ARCHITECTURE.md"):
            delivery_text += path.read_text()
    lower = delivery_text.lower()
    assert "at-least-once" in lower or "at least once" in lower
