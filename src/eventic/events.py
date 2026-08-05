"""
eventic.events  ──  Event-based decorators for Record lifecycle hooks

Handlers are keyed by the **class object** (not the class name), so two
classes that happen to share a ``__name__`` never cross-fire, and handler
order within a class is registration order.

Timing: a create handler runs *after* the v0 row is persisted; an update
handler runs after the append but *before* the transaction commits — treat
the store as eventually-consistent within the emitting transaction.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable, Dict, List, Type

logger = logging.getLogger(__name__)


class EventRegistry:
    """Central registry for event handlers."""

    def __init__(self):
        # event_type -> record class object -> ordered list of handlers
        self._handlers: Dict[str, Dict[type, List[Callable]]] = {
            "create": defaultdict(list),
            "update": defaultdict(list),
        }

    def register(
        self,
        event_type: str,
        record_classes: tuple,
        handler: Callable,
    ) -> None:
        """Register a handler for specific record classes."""
        for cls in record_classes:
            self._handlers[event_type][cls].append(handler)

    def emit(self, event_type: str, instance) -> None:
        """Emit event to all matching handlers (base classes too)."""
        for cls in instance.__class__.__mro__:
            for handler in self._handlers[event_type].get(cls, []):
                try:
                    handler(instance)
                except Exception:
                    # Isolation policy: a failing handler must not break the
                    # mutation/construction that emitted the event.
                    logger.exception(
                        "event handler %s failed for %s(%s)",
                        handler.__name__,
                        instance.__class__.__name__,
                        instance.id,
                    )


# Global registry instance
_registry = EventRegistry()


class OnDecorator:
    """Namespace for event decorators"""

    @staticmethod
    def create(*record_classes: Type) -> Callable:
        """Decorator for handling record creation events"""

        def decorator(func: Callable) -> Callable:
            _registry.register("create", record_classes, func)
            return func

        return decorator

    @staticmethod
    def update(*record_classes: Type) -> Callable:
        """Decorator for handling record update events"""

        def decorator(func: Callable) -> Callable:
            _registry.register("update", record_classes, func)
            return func

        return decorator


# Export the decorator interface
on = OnDecorator()


# Hook into Record lifecycle
def emit_create(instance) -> None:
    """Emit create event for new instances."""
    _registry.emit("create", instance)


def emit_update(instance) -> None:
    """Emit update event for modified instances."""
    _registry.emit("update", instance)
