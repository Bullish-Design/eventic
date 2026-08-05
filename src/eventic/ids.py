"""Identity primitives: stream names, aggregate keys, revision ids.

``revision_id`` is the single identity function for a revision. It must never
change; ``NS`` is fixed and checked in on purpose.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

from pydantic import BeforeValidator

# Fixed namespace; changing it changes every revision id ever produced.
NS = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

_STREAM_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def _validate_stream_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("stream name must be a string")
    if not _STREAM_NAME_RE.match(value):
        raise ValueError(
            f"stream name must match [a-z0-9][a-z0-9_.-]{{0,63}} (got {value!r})"
        )
    return value


StreamName = Annotated[str, BeforeValidator(_validate_stream_name)]


@dataclass(frozen=True, slots=True)
class AggregateKey:
    """The aggregate identity: ``(stream, aggregate_id)``. Hashable."""

    stream: str
    aggregate_id: UUID

    def __post_init__(self) -> None:
        object.__setattr__(self, "stream", _validate_stream_name(self.stream))


def revision_id(stream: str, aggregate_id: UUID, revision: int) -> UUID:
    """Deterministic revision identity: ``uuid5(NS, f"{stream}:{id}:{rev}")``."""
    return uuid.uuid5(NS, f"{stream}:{aggregate_id}:{revision}")
