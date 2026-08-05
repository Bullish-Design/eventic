"""The outbox worker: claim -> deliver (outside any transaction) -> settle.

At-least-once. A side effect may succeed and the ack may fail, so handlers
must be idempotent. The worker holds no database lock while user code runs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from eventic.app import App
from eventic.envelopes import Commit
from eventic.errors import DeliveryError
from eventic.hydration import hydrate
from eventic.ids import AggregateKey
from eventic.jsonx import JsonObject
from eventic.planning import changed_keys
from eventic.protocols import Store
from eventic.retry import disposition
from eventic.stream import Stream
from eventic.wire import ClaimedIntent, Settlement

logger = logging.getLogger("eventic.worker")


@dataclass
class WorkerReport:
    claimed: int = 0
    delivered: int = 0
    retried: int = 0
    dead_lettered: int = 0


class Worker:
    """Drains one queue; ``drain_once`` is one claim/deliver/settle batch."""

    def __init__(
        self,
        app: App,
        store: Store,
        *,
        queue: str = "default",
        lease: timedelta = timedelta(seconds=30),
        batch_size: int = 100,
    ) -> None:
        self._app = app
        self._store = store
        self._queue = queue
        self._lease = lease
        self._batch_size = batch_size
        self._subscriptions = {sub.id: sub for sub in app.subscriptions}

    def drain_once(self) -> WorkerReport:
        claimed = self._store.claim(
            self._queue, limit=self._batch_size, lease=self._lease
        )
        if not claimed:
            return WorkerReport()
        report = WorkerReport(claimed=len(claimed))
        settlements: list[Settlement] = []
        for intent in claimed:
            self._deliver(intent, settlements, report)
        self._store.settle(settlements)
        return report

    def run_forever(self, *, poll: timedelta = timedelta(seconds=1)) -> None:
        """Drain in a loop; returns only on interruption."""
        while True:
            self.drain_once()
            import time

            time.sleep(poll.total_seconds())

    # -- internals -----------------------------------------------------------

    def _deliver(
        self, intent: ClaimedIntent, settlements: list[Settlement], report: WorkerReport
    ) -> None:
        subscription = self._subscriptions.get(intent.subscription_id)
        if subscription is None:
            self._dead_letter(
                intent,
                settlements,
                report,
                DeliveryError(f"unknown subscription {intent.subscription_id}"),
            )
            return
        try:
            commit = self._reconstruct(subscription.stream, intent)
            subscription.handler(commit)
        except Exception as exc:  # noqa: BLE001
            self._retry_or_dead(subscription, intent, settlements, report, exc)
            return
        settlements.append(Settlement(intent_id=intent.intent_id, status="delivered"))
        report.delivered += 1

    def _reconstruct(
        self, stream: Stream[Any], intent: ClaimedIntent
    ) -> Commit[Any, Any]:
        if intent.aggregate_id is None or intent.revision < 0:
            raise DeliveryError("claimed intent lacks an aggregate key")
        key = AggregateKey(intent.stream, intent.aggregate_id)
        stored = self._store.revision(key, intent.revision)
        if stored is None:
            raise DeliveryError(
                f"revision {intent.revision} of {intent.stream} is absent"
            )
        revision = hydrate(stream, self._app.meta, stored)
        changed = self._changed_for(stream, intent, stored.payload)
        return Commit[Any, Any](
            kind=stored.kind,  # type: ignore[arg-type]
            revision=revision,
            changed=changed,
        )

    def _changed_for(
        self, stream: Stream[Any], intent: ClaimedIntent, after: JsonObject
    ) -> frozenset[str]:
        if intent.revision == 0 or intent.aggregate_id is None:
            return frozenset(after)
        previous = self._store.revision(
            AggregateKey(intent.stream, intent.aggregate_id), intent.revision - 1
        )
        if previous is None:
            return frozenset(after)
        return changed_keys(previous.payload, after)

    def _retry_or_dead(
        self,
        subscription: Any,
        intent: ClaimedIntent,
        settlements: list[Settlement],
        report: WorkerReport,
        exc: Exception,
    ) -> None:
        backoff = (
            subscription.delivery.retry
            if hasattr(subscription.delivery, "retry")
            else None
        )
        if backoff is None:
            from eventic.subscription import Backoff

            backoff = Backoff()
        now = datetime.now(UTC)
        decision = disposition(intent.attempts, backoff, exc, now)
        if decision.action == "dead":
            self._dead_letter(intent, settlements, report, exc)
            return
        settlements.append(
            Settlement(
                intent_id=intent.intent_id,
                status="retry",
                available_at=decision.available_at,
                error=decision.error,
            )
        )
        report.retried += 1

    def _dead_letter(
        self,
        intent: ClaimedIntent,
        settlements: list[Settlement],
        report: WorkerReport,
        exc: Exception,
    ) -> None:
        from eventic.retry import redact_error

        settlements.append(
            Settlement(
                intent_id=intent.intent_id,
                status="dead",
                error=redact_error(exc),
            )
        )
        report.dead_lettered += 1
