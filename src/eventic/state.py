"""User-state helpers (CONCEPT §5.1).

``data`` holds the codec's output and **nothing else**: user fields only.
Managed fields (``id``/``version``/``version_id``) and commit metadata
(``committed_at`` → ``created_ts``) live in columns and are merged back at
hydration. Splitting state from commit metadata is what makes I5 (byte-
identical replays) coexist with F5 (``created_ts`` reflects the commit time):
a crash-recovery replay compares stable bytes because no timestamp ever sits
inside ``data``.
"""

from __future__ import annotations

from typing import Any

MANAGED = frozenset({"id", "version", "version_id", "created_ts"})


def user_state(rec) -> dict[str, Any]:
    """The JSON-safe user state of ``rec`` — managed fields excluded."""
    return rec.model_dump(mode="json", exclude=MANAGED)
