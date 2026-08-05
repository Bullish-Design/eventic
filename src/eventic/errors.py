"""The full exception tree.

Every public error derives from :class:`EventicError`. Errors carry structured
attributes where relevant; no message ever interpolates a payload, a credential,
or a connection URL.
"""

from __future__ import annotations

from typing import Any


class EventicError(Exception):
    """Base class for every error raised by eventic."""

    def __init__(
        self,
        message: str,
        *,
        stream: str | None = None,
        aggregate_id: Any = None,
        revision: int | None = None,
        subscription_id: str | None = None,
        **attrs: Any,
    ) -> None:
        self.stream = stream
        self.aggregate_id = aggregate_id
        self.revision = revision
        self.subscription_id = subscription_id
        self.attrs = attrs
        super().__init__(message)


class ConfigError(EventicError):
    """A declaration is invalid: caught at ``App`` / ``Stream`` construction."""


class DuplicateId(ConfigError):
    """Two streams or subscriptions in one ``App`` share a durable id."""


class UnknownStream(ConfigError):
    """A subscription references a stream not installed in the ``App``."""


class UnsupportedHandler(ConfigError):
    """A subscription handler is async or has an incompatible signature."""


class IncompleteUpcasterChain(ConfigError):
    """A stream or meta declaration cannot upcast from its stored version."""


class UsageError(EventicError):
    """The public API was misused."""


class NotFound(EventicError):
    """An aggregate or exact revision is absent."""


class RevisionConflict(EventicError):
    """Compare-and-swap failed: stale, fabricated, or concurrent write."""


class EncodingError(EventicError):
    """An encoder and decoder disagree about a physical payload."""


class UndecodableRevision(EventicError):
    """Round-trip verification or upcast failed for a stored revision."""

    def __init__(
        self,
        message: str,
        *,
        pointer: str | None = None,
        **attrs: Any,
    ) -> None:
        self.pointer = pointer
        super().__init__(message, **attrs)


class CapabilityUnsupported(EventicError):
    """The bound store lacks a capability a declaration requires."""


class StoreError(EventicError):
    """A backend failure, translated at the store boundary."""


class DeliveryError(EventicError):
    """A delivery failure surfaced to the writing process."""


class InlineDispatchError(DeliveryError):
    """One or more inline handlers raised; the commit is already durable."""


class DeadLettered(DeliveryError):
    """A subscription intent exhausted its retries and is dead."""
