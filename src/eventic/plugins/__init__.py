"""The closed five-seam plugin framework (PLUGINS.md).

``Plugin`` base + ``Seam`` enum (exactly five seam kinds), the capability-token
contract (``provides``/``requires``), the global delivery mode registry, the
per-app ``use()`` defaults, and ``assemble()`` — the class assembler that
validates a ``Record`` subclass's plugin set **at class definition**, never at
import or first call (kills the same-name-class / import-time side effects).

The defaults are themselves plugins (CONCEPT §7): ``SingleTableJSONB``,
``FullSnapshot``, ``Uuid5Deterministic``, ``SyncDelivery``. A chosen provider
*replaces* the default for its seam — and the default's capabilities stop
counting (D7), so ``DiffStorage + TypedTable`` fails the ``requires`` check
even though the JSON default persistence exists.
"""

from __future__ import annotations

import enum
from collections import defaultdict
from typing import Any, Iterable

from ..errors import MissingCapability, PluginConflictError


class Seam(str, enum.Enum):
    PERSISTENCE = "persistence"
    CODEC = "codec"
    IDENTITY = "identity"
    DELIVERY = "delivery"
    INTERCEPTOR = "interceptor"


EXCLUSIVE = {Seam.PERSISTENCE, Seam.CODEC, Seam.IDENTITY}


class Plugin:
    """A provider occupying one seam of the write/read pipeline.

    ``provides``/``requires`` are capability tokens (e.g. ``"persistence:json"``);
    ``priority`` orders stacking seams; ``mode`` names a delivery backend.
    """

    seam: Seam
    provides: set[str] = set()
    requires: set[str] = set()
    priority: int = 0
    mode: str | None = None  # delivery backends only


# ---------------------------------------------------------------------- #
# delivery mode registry (mode name -> exactly one backend class)
# ---------------------------------------------------------------------- #
_DELIVERY_MODES: dict[str, type[Plugin]] = {}
_DELIVERY_INSTANCES: dict[str, Any] = {}


def register_delivery(plugin_cls: type[Plugin]) -> None:
    """Register (or idempotently re-confirm) the backend for ``plugin_cls.mode``."""
    mode = plugin_cls.mode
    if mode is None:
        raise PluginConflictError(
            f"{plugin_cls.__name__} occupies the delivery seam but declares no mode"
        )
    existing = _DELIVERY_MODES.get(mode)
    if existing is not None and existing is not plugin_cls:
        raise PluginConflictError(
            f"delivery mode {mode!r} already registered by {existing.__name__}; "
            f"cannot also use {plugin_cls.__name__}"
        )
    _DELIVERY_MODES[mode] = plugin_cls
    _DELIVERY_INSTANCES[mode] = plugin_cls()


def delivery_backends() -> list[Any]:
    """Every registered delivery backend (``sync`` always exists)."""
    if "sync" not in _DELIVERY_INSTANCES:
        from .delivery import SyncDelivery

        register_delivery(SyncDelivery)
    return list(_DELIVERY_INSTANCES.values())


def _reset_delivery() -> None:
    """Test hook: clear the mode registry."""
    _DELIVERY_MODES.clear()
    _DELIVERY_INSTANCES.clear()


# ---------------------------------------------------------------------- #
# per-app global plugin defaults (eventic.use)
# ---------------------------------------------------------------------- #
_GLOBAL_PLUGINS: list[type[Plugin]] = []


def use(*plugins: type[Plugin]) -> None:
    """Make plugins the default for every subsequently-defined Record class."""
    for p in plugins:
        _GLOBAL_PLUGINS.append(p)


def _reset_globals() -> None:
    _GLOBAL_PLUGINS.clear()


# ---------------------------------------------------------------------- #
# the class assembler
# ---------------------------------------------------------------------- #
def assemble(cls: type, plugin_classes: Iterable[type[Plugin]]) -> None:
    """Validate ``plugin_classes`` for ``cls`` and attach the seam providers.

    Raises ``PluginConflictError`` (two providers on one exclusive seam) or
    ``MissingCapability`` (an unmet ``requires`` token) **at class
    definition**. Every exclusive seam is filled by the chosen provider or its
    default; the effective set's ``provides`` are what ``requires`` may count
    on (D7).
    """
    plugin_classes = [p for p in plugin_classes if issubclass(p, Plugin)]
    chosen: dict[Seam, list[type[Plugin]]] = defaultdict(list)
    for p in plugin_classes:
        chosen[p.seam].append(p)

    for seam in EXCLUSIVE:
        if len(chosen[seam]) > 1:
            raise PluginConflictError(
                f"{cls.__name__}: multiple {seam.value} providers: "
                f"{[p.__name__ for p in chosen[seam]]}"
            )

    def effective(seam: Seam, default: type[Plugin]) -> type[Plugin]:
        return (chosen[seam] or [default])[0]

    # the effective provider set — a chosen provider REPLACES its default
    pool = [
        effective(Seam.PERSISTENCE, SingleTableJSONB),
        effective(Seam.CODEC, FullSnapshot),
        effective(Seam.IDENTITY, Uuid5Deterministic),
        *chosen[Seam.DELIVERY],
        *chosen[Seam.INTERCEPTOR],
    ]
    provided: set[str] = set()
    for p in pool:
        provided |= set(p.provides)
    for p in pool:
        missing = set(p.requires) - provided
        if missing:
            raise MissingCapability(
                f"{cls.__name__}: {p.__name__} requires {sorted(missing)} — "
                f"none of the assembled providers offer them"
            )

    cls.__eventic_plugins__ = list(plugin_classes)
    cls._persistence = effective(Seam.PERSISTENCE, SingleTableJSONB)()
    cls._codec = effective(Seam.CODEC, FullSnapshot)()
    cls._identity = effective(Seam.IDENTITY, Uuid5Deterministic)()
    cls._interceptors = sorted(
        (p() for p in chosen[Seam.INTERCEPTOR]), key=lambda p: p.priority
    )

    for p in chosen[Seam.DELIVERY]:
        register_delivery(p)
    if "sync" not in _DELIVERY_MODES:
        from .delivery import SyncDelivery

        register_delivery(SyncDelivery)


# defaults are themselves plugins — import them last so the submodules can
# safely `from . import Plugin, Seam` (no circular import)
from .codec import FullSnapshot  # noqa: E402
from .delivery import SyncDelivery  # noqa: E402
from .identity import Uuid5Deterministic  # noqa: E402
from .persistence import SingleTableJSONB  # noqa: E402
