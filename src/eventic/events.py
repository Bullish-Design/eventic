"""
eventic.events  ──  Event-based decorators for Record lifecycle hooks
"""

from __future__ import annotations
from typing import Callable, Type, Set, Dict, Any, TYPE_CHECKING
from functools import wraps
from collections import defaultdict

if TYPE_CHECKING:
    from .core.record import Record


class EventRegistry:
    """Central registry for event handlers"""

    def __init__(self):
        # Maps event type -> record class -> set of handlers
        self._handlers: Dict[str, Dict[Type[Record], Set[Callable]]] = {
            "create": defaultdict(set),
            "update": defaultdict(set),
        }
        # Maps class name -> classes to check
        self._class_map: Dict[str, Set[type]] = defaultdict(set)

    def register(
        self,
        event_type: str,
        record_classes: tuple[Type[Record], ...],
        handler: Callable,
    ) -> None:
        """Register a handler for specific record classes"""
        for cls in record_classes:
            class_name = cls.__name__
            self._handlers[event_type][class_name].add(handler)
            self._class_map[class_name].add(cls)

    def emit(self, event_type: str, instance: Record) -> None:
        """Emit event to all matching handlers"""
        instance_class = instance.__class__
        handlers = set()

        # Also check parent classes
        for cls in instance_class.__mro__:
            class_name = cls.__name__
            if class_name in self._handlers[event_type]:
                handlers.update(self._handlers[event_type][class_name])

        for handler in handlers:
            handler(instance)


# Global registry instance
_registry = EventRegistry()


class OnDecorator:
    """Namespace for event decorators"""

    @staticmethod
    def create(*record_classes: Type[Record]) -> Callable:
        """Decorator for handling record creation events"""

        def decorator(func: Callable) -> Callable:
            _registry.register("create", record_classes, func)
            return func

        return decorator

    @staticmethod
    def update(*record_classes: Type[Record]) -> Callable:
        """Decorator for handling record update events"""

        def decorator(func: Callable) -> Callable:
            _registry.register("update", record_classes, func)
            return func

        return decorator


# Export the decorator interface
on = OnDecorator()


# Hook into Record lifecycle
def emit_create(instance: Record) -> None:
    """Emit create event for new instances"""
    _registry.emit("create", instance)


def emit_update(instance: Record) -> None:
    """Emit update event for modified instances"""
    _registry.emit("update", instance)
