"""Phase 4 gate: importing every eventic module defines no module-level mutable
binding (R9) — private names included.

The three globals that caused this library's entire redesign were named
``_ENGINE`` (003/F8), ``_CURRENT`` (003/F10, 004/F16) and activation tokens:
conventionally private, and previously invisible because the scan skipped
every ``_``-prefixed name (F4). A module-level cache cannot be added without
this test failing, whatever it is named.

Scope, deliberately:
- dunder names (``__builtins__``, ``__path__``, ``__all__``) are interpreter
  machinery, not library state — exempted.
- ``model_config`` on pydantic models is the framework's declarative
  configuration slot, not mutable library state — exempted.
- class attributes are scanned only for classes *defined in the scanned
  module*; imported third-party classes (pydantic's ``SecretStr``,
  SQLAlchemy's ``Insert``) carry their own internals we neither own nor
  mutate.
- module-level *objects* whose attributes are mutable (e.g. the frozen
  ``NoMeta`` dataclass with its empty ``upcasters`` mapping) are out of
  scope for a mechanical scan: immutability is not structurally typed for
  arbitrary objects, and the historically shipped bug was always a dict or
  set reachable by name. A reviewer reading ``OUTCOME.md`` for 007 Phase 4
  accepts that trade-off; the shapes that matter are covered.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys

import eventic


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


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


def _module_mutables() -> list[str]:
    """Names of module-level mutable bindings, any name shape (private
    included), plus class-level mutable defaults on module-defined classes."""
    offenders: list[str] = []
    for module in _all_modules():
        for name, value in vars(module).items():
            if _is_dunder(name):
                continue
            if isinstance(value, (dict, list, set)):
                offenders.append(f"{module.__name__}.{name}")
            if isinstance(value, type) and value.__module__ == module.__name__:
                for attr, attr_value in vars(value).items():
                    if attr.startswith("__") or attr == "model_config":
                        continue
                    if isinstance(attr_value, (dict, list, set)):
                        offenders.append(f"{module.__name__}.{name}.{attr}")
    return sorted(offenders)


def test_no_module_level_mutable_binding() -> None:
    offenders = _module_mutables()
    assert not offenders, "module-level mutable bindings found (R9): " + ", ".join(
        offenders
    )


def test_scan_catches_an_injected_private_module_mutable() -> None:
    """The guard must see the exact shape the 003/004 bugs shipped in.

    ``planning._CURRENT_STORE = {}`` — the literal shape of 003/F8's
    ``_ENGINE`` and 004/F16's ``_CURRENT`` — used to be invisible because the
    scan skipped every ``_``-prefixed name. Inject it, prove the scan reports
    it, then remove it.
    """
    import eventic.planning as planning

    assert "_CURRENT_STORE" not in vars(planning)
    planning._CURRENT_STORE = {}  # type: ignore[attr-defined]
    try:
        offenders = _module_mutables()
        assert "eventic.planning._CURRENT_STORE" in offenders, offenders
    finally:
        del planning._CURRENT_STORE  # type: ignore[attr-defined]
    assert "eventic.planning._CURRENT_STORE" not in _module_mutables()
