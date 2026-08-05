"""Inline dispatch: best-effort, in-process, after ``COMMIT`` returns (I9).

Every matching handler runs even if an earlier one raises; failures are
collected and re-raised as :class:`InlineDispatchError` (default) or logged
per ``App.on_inline_error``.
"""

from __future__ import annotations

import logging
from typing import Any

from eventic.app import App
from eventic.envelopes import Commit
from eventic.errors import InlineDispatchError
from eventic.stream import Stream
from eventic.subscription import Inline

logger = logging.getLogger("eventic")


def dispatch_inline(app: App, stream: Stream[Any], commit: Commit[Any, Any]) -> None:
    """Run every inline subscription matching this commit, in declaration order."""
    failures: list[str] = []
    for sub in app.subscriptions:
        if sub.stream.name != stream.name or commit.kind not in sub.kinds:
            continue
        if not isinstance(sub.delivery, Inline):
            continue
        try:
            sub.handler(commit)
        except Exception as exc:  # noqa: BLE001
            message = f"subscription {sub.id}: {type(exc).__name__}: {exc}"
            if app.on_inline_error == "log":
                logger.exception("inline handler failed for %s", sub.id)
            else:
                failures.append(message)
    if failures:
        raise InlineDispatchError("\n".join(failures))
