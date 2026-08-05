"""``App`` — an immutable, validated declaration.

Not a registry, not a service locator, not a compiler: a frozen value whose
constructor is the validator. Every declaration error is collected and raised
together, one line per failure.
"""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal, get_origin

from pydantic import BaseModel, ConfigDict, model_validator

from eventic.errors import (
    CapabilityUnsupported,
    ConfigError,
    DuplicateId,
    UnknownStream,
    UnsupportedHandler,
)
from eventic.meta import Meta, NoMeta
from eventic.stream import Stream
from eventic.subscription import Outbox, Subscription

if TYPE_CHECKING:
    from eventic.protocols import Store
    from eventic.runtime import Runtime

InlineErrorMode = Literal["raise", "log"]


def _handler_problems(sub: Subscription[Any, Any]) -> list[str]:
    problems: list[str] = []
    handler = sub.handler
    if inspect.iscoroutinefunction(handler):
        problems.append(
            f"subscription {sub.id}: async handlers are not supported in 1.0; "
            "declare a sync handler"
        )
    try:
        params = [
            p
            for p in inspect.signature(handler).parameters.values()
            if p.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
    except (TypeError, ValueError):
        params = []
    if len(params) != 1:
        problems.append(
            f"subscription {sub.id}: handler must accept exactly one positional "
            f"argument (a Commit[T, M]); got {len(params)}"
        )
        return problems
    # Annotation check is best-effort: string annotations that reference
    # function-local names cannot be evaluated, and an untyped handler is
    # acceptable (dispatch is dynamic).
    try:
        hints = inspect.get_annotations(handler, eval_str=True)
    except (TypeError, ValueError, NameError):
        hints = {}
    annotation = hints.get(params[0].name, inspect.Parameter.empty)
    if annotation is not inspect.Parameter.empty and not _is_commit(annotation):
        problems.append(
            f"subscription {sub.id}: handler argument must be typed as "
            f"Commit[T, M] (got {annotation!r})"
        )
    return problems


def _is_commit(t: object) -> bool:
    from eventic.envelopes import Commit

    return (
        t is Commit
        or get_origin(t) is Commit
        or (isinstance(t, type) and issubclass(t, Commit))
    )


class App(BaseModel):
    """An immutable, validated application declaration."""

    model_config = ConfigDict(
        frozen=True,
        arbitrary_types_allowed=True,
        extra="forbid",
    )

    id: str
    streams: Sequence[Stream[Any]] = ()
    meta: Meta[Any] = NoMeta
    subscriptions: Sequence[Subscription[Any, Any]] = ()
    on_inline_error: InlineErrorMode = "raise"

    @model_validator(mode="after")
    def _validate(self) -> App:
        problems: list[tuple[type[ConfigError], str]] = []

        if not self.id:
            problems.append((ConfigError, "app id must be non-empty"))

        stream_names: set[str] = set()
        for stream in self.streams:
            if stream.name in stream_names:
                problems.append((DuplicateId, f"duplicate stream name: {stream.name}"))
            stream_names.add(stream.name)

        sub_ids: set[str] = set()
        for sub in self.subscriptions:
            if sub.id in sub_ids:
                problems.append((DuplicateId, f"duplicate subscription id: {sub.id}"))
            sub_ids.add(sub.id)
            if sub.stream.name not in stream_names:
                problems.append(
                    (
                        UnknownStream,
                        f"subscription {sub.id}: stream {sub.stream.name} is not "
                        "installed in this app",
                    )
                )
            problems.extend((UnsupportedHandler, msg) for msg in _handler_problems(sub))

        if problems:
            # §2.1: all checks run and are reported together. If every failure
            # is the same fault class, raise that class so callers can catch
            # it specifically; heterogeneous failures raise the common base
            # (F6).
            message = "\n".join(msg for _, msg in problems)
            classes = {cls for cls, _ in problems}
            if len(classes) == 1:
                raise classes.pop()(message)
            raise ConfigError(message)
        object.__setattr__(self, "streams", tuple(self.streams))
        object.__setattr__(self, "subscriptions", tuple(self.subscriptions))
        return self

    def bind(self, store: Store) -> Runtime:
        """Capability check, then a ``Runtime`` bound to ``store``.

        Opens no connection.
        """
        outbox_needed = any(
            isinstance(sub.delivery, Outbox) for sub in self.subscriptions
        )
        if outbox_needed and not store.capabilities.outbox:
            raise CapabilityUnsupported(
                "app declares Outbox subscriptions but the store does not "
                "support transactional delivery intents",
            )
        from eventic.runtime import Runtime

        return Runtime(app=self, store=store)  # type: ignore[assignment]
