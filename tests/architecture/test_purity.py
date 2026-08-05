"""Phase 5 gate: the pure core imports nothing that could perform I/O or read
the clock (R3)."""

from __future__ import annotations

import ast
from pathlib import Path

FORBIDDEN = {"sqlalchemy", "os", "time", "random", "socket", "requests", "httpx"}

PURE_MODULES = ["wire", "planning", "hydration", "retry"]


def _module_imports(name: str) -> list[str]:
    path = Path("src/eventic") / f"{name}.py"
    tree = ast.parse(path.read_text())
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    return imports


def test_pure_modules_import_nothing_dangerous() -> None:
    for name in PURE_MODULES:
        for module in _module_imports(name):
            top = module.split(".")[0]
            assert top not in FORBIDDEN, f"{name}.py imports {module}"


def test_pure_modules_do_not_read_clock() -> None:
    for name in PURE_MODULES:
        source = Path("src/eventic") / f"{name}.py"
        text = source.read_text()
        assert "datetime.now" not in text, f"{name}.py calls datetime.now"
        assert "time.time" not in text, f"{name}.py calls time.time"
        assert "uuid4" not in text and "uuid.uuid4" not in text, (
            f"{name}.py calls uuid4"
        )
