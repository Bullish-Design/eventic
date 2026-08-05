"""Phase 4 gate: importing every eventic module defines no module-level mutable
binding (R9)."""

from __future__ import annotations

import importlib
import pkgutil
import sys

import eventic


def _all_modules() -> list[object]:
    modules = [eventic]
    for info in pkgutil.walk_packages(eventic.__path__, eventic.__name__ + "."):
        if info.name == "eventic.sql.migrations" or info.name.startswith(
            "eventic.sql.migrations."
        ):
            continue  # alembic scripts are not runtime modules (R9-free by design)
        importlib.import_module(info.name)
        modules.append(sys.modules[info.name])
    return modules


def test_no_module_level_mutable_binding() -> None:
    offenders: list[str] = []
    for module in _all_modules():
        for name, value in vars(module).items():
            if name.startswith("_"):
                continue
            if isinstance(value, (dict, list, set)):
                offenders.append(f"{module.__name__}.{name}")
    assert not offenders, "module-level mutable bindings found (R9): " + ", ".join(
        offenders
    )
