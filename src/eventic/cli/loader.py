"""``--app module:attr`` loading. A load failure is a clear error with a
non-zero exit — never a silent no-op (004/F13).
"""

from __future__ import annotations

import importlib
from typing import Any

from eventic.app import App
from eventic.errors import ConfigError


def load_app(spec: str) -> App:
    """Import ``module:attr`` and return the ``App`` value it names."""
    if ":" not in spec:
        raise ConfigError(f"--app must be 'module:attr' (got {spec!r})")
    module_name, attr = spec.split(":", 1)
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ConfigError(f"cannot import app module {module_name!r}: {exc}") from exc
    if not hasattr(module, attr):
        raise ConfigError(f"module {module_name!r} has no attribute {attr!r}")
    value: Any = getattr(module, attr)
    if not isinstance(value, App):
        raise ConfigError(
            f"{spec!r} does not name an eventic App (got {type(value).__name__})"
        )
    return value


def make_store(url: str, *, create_tables: bool = True) -> Any:
    """Build a store from a URL: sqlite:// or postgresql:// ."""
    if url.startswith("sqlite"):
        from eventic.sql import SQLite

        return SQLite(url, create_tables=create_tables)
    if url.startswith("postgresql"):
        from eventic.sql import Postgres

        return Postgres(url, create_tables=create_tables)
    raise ConfigError(
        "unsupported database URL scheme (expected sqlite:// or postgresql://)"
    )
