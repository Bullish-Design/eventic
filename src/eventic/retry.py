"""Pure retry decisions: attempt + error -> ``Disposition``.

No clock read, no randomness: the current time is an argument.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from eventic.subscription import Backoff

_MAX_ERROR_LENGTH = 2048  # 2 KiB, per ARCHITECTURE.md §6.3

_CREDENTIALED_URL = re.compile(r"(\w+://)[^/@\s]+@", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Disposition:
    """What to do with a failed delivery: retry later or dead-letter."""

    action: str  # "retry" | "dead"
    available_at: datetime | None = None
    error: str | None = None


def disposition(
    attempts: int,
    backoff: Backoff,
    error: object,
    now: datetime,
) -> Disposition:
    """Decide retry vs dead for a delivery that failed on its ``attempts``-th try."""
    if attempts >= backoff.max_attempts:
        return Disposition(action="dead", error=redact_error(error))
    delay = min(backoff.base * (backoff.factor ** (attempts - 1)), backoff.cap)
    return Disposition(action="retry", available_at=now + timedelta(seconds=delay))


def redact_error(error: object) -> str:
    """A safe, truncated error string: no credentials, no URLs, <= 2 KiB."""
    text = str(error)
    text = _CREDENTIALED_URL.sub(r"\1***@", text)
    return text[:_MAX_ERROR_LENGTH]
