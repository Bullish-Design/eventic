"""The sync conformance runner.

A scenario is executed against a fresh store produced by ``factory``. The
runner asserts every expected outcome and reports failures with the scenario
name. The future async runner is a second file sharing this vocabulary.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from eventic.errors import EventicError, RevisionConflict
from eventic.ids import AggregateKey
from eventic.jsonx import JsonObject
from eventic.protocols import Capabilities, Store
from eventic.testing.conformance import scenarios as scenario_data
from eventic.testing.conformance.store import (
    Batch,
    Claim,
    Commit,
    ConcurrentDrainers,
    Exact,
    Head,
    History,
    Race,
    Scenario,
    Search,
    Settle,
    Step,
    Time,
    Wait,
)
from eventic.wire import (
    ClaimedIntent,
    CommitRequest,
    Settlement,
    StoredRevision,
)


class StepFailure(AssertionError):
    pass


@dataclass
class ScenarioResult:
    name: str
    failures: list[str] = field(default_factory=list)
    skipped_reason: str | None = None

    @property
    def passed(self) -> bool:
        return not self.failures and self.skipped_reason is None

    @property
    def skipped(self) -> bool:
        return self.skipped_reason is not None


class _Context:
    def __init__(self) -> None:
        self.last_claimed: list[ClaimedIntent] = []
        self.last_commit_stamps: set[datetime] = set()


def _capability_names(caps: Capabilities) -> frozenset[str]:
    return frozenset(
        name for name, value in dataclasses.asdict(caps).items() if value is True
    )


def _to_request(commit: Commit) -> CommitRequest:
    return CommitRequest(
        stream=commit.stream,
        aggregate_id=commit.aggregate_id,
        expected_revision=commit.expected_revision,
        kind=commit.kind,  # type: ignore[arg-type]
        schema_version=commit.schema_version,
        payload=commit.payload,
        digest=commit.digest,
        meta=commit.meta,
        meta_version=commit.meta_version,
        fingerprint=commit.fingerprint,
        intents=commit.intents,
    )


def _check_revision(
    expected: StoredRevision | None,
    *,
    expect_missing: bool,
    expect_revision: int | None,
    expect_digest: str | None,
    expect_payload: JsonObject | None,
) -> None:
    if expect_missing:
        if expected is not None:
            raise StepFailure(f"expected no revision, got revision {expected.revision}")
        return
    if expected is None:
        raise StepFailure("expected a revision, got none")
    if expect_revision is not None and expected.revision != expect_revision:
        raise StepFailure(f"revision {expected.revision} != expected {expect_revision}")
    if expect_digest is not None and expected.digest != expect_digest:
        raise StepFailure("digest mismatch")
    if expect_payload is not None and expected.payload != expect_payload:
        raise StepFailure(
            f"payload {expected.payload!r} != expected {expect_payload!r}"
        )
    if not isinstance(expected.committed_at, datetime):  # type: ignore[reportUnnecessaryIsInstance]
        raise StepFailure("committed_at is not a datetime")


def _expect_raise(fn: Callable[[], Any], error_name: str | None) -> None:
    try:
        fn()
    except EventicError as exc:
        if error_name is None:
            raise StepFailure(f"unexpected {type(exc).__name__}: {exc}") from exc
        if type(exc).__name__ != error_name:
            raise StepFailure(
                f"raised {type(exc).__name__}, expected {error_name}"
            ) from exc
        return
    except Exception as exc:  # noqa: BLE001
        raise StepFailure(
            f"driver/foreign exception escaped: {type(exc).__name__}: {exc}"
        ) from exc
    if error_name is not None:
        raise StepFailure(f"expected {error_name}, nothing raised")


def _run_step(store: Store, ctx: _Context, step: Step) -> None:
    if isinstance(step, Commit):
        request = _to_request(step)
        if step.expect_error:
            _expect_raise(lambda: store.commit([request]), step.expect_error)
            return
        results = store.commit([request])
        ctx.last_commit_stamps = {result.committed_at for result in results}
        if len(results) != 1:
            raise StepFailure(f"commit returned {len(results)} results")
        result = results[0]
        if step.expect_revision is not None and result.revision != step.expect_revision:
            raise StepFailure(
                f"committed revision {result.revision} != {step.expect_revision}"
            )
        if step.expect_replayed is not None and result.replayed != step.expect_replayed:
            raise StepFailure(
                f"replayed={result.replayed}, expected {step.expect_replayed}"
            )
        if not isinstance(result.committed_at, datetime):  # type: ignore[reportUnnecessaryIsInstance]
            raise StepFailure("committed_at is not a datetime")
        return

    if isinstance(step, Batch):
        requests = [_to_request(c) for c in step.commits]
        if step.expect_error:
            _expect_raise(lambda: store.commit(requests), step.expect_error)
            return
        results = store.commit(requests)
        ctx.last_commit_stamps = {result.committed_at for result in results}
        if len(results) != len(requests):
            raise StepFailure(
                f"batch returned {len(results)} results for {len(requests)} requests"
            )
        for commit, result in zip(step.commits, results, strict=True):
            if (
                commit.expect_revision is not None
                and result.revision != commit.expect_revision
            ):
                raise StepFailure(
                    "batch result revision "
                    f"{result.revision} != {commit.expect_revision}"
                )
        return

    if isinstance(step, Head):
        value = store.head(AggregateKey(step.stream, step.aggregate_id))
        _check_revision(
            value,
            expect_missing=step.expect_missing,
            expect_revision=step.expect_revision,
            expect_digest=step.expect_digest,
            expect_payload=step.expect_payload,
        )
        return

    if isinstance(step, Exact):
        value = store.revision(
            AggregateKey(step.stream, step.aggregate_id), step.revision
        )
        _check_revision(
            value,
            expect_missing=step.expect_missing,
            expect_revision=None,
            expect_digest=step.expect_digest,
            expect_payload=step.expect_payload,
        )
        return

    if isinstance(step, History):
        page = store.history(
            AggregateKey(step.stream, step.aggregate_id),
            after=step.after,
            limit=step.limit,
        )
        revisions = tuple(item.revision for item in page.items)
        if revisions != step.expect_revisions:
            raise StepFailure(
                f"history revisions {revisions} != {step.expect_revisions}"
            )
        payloads = tuple(item.payload for item in page.items)
        if step.expect_payloads and payloads != step.expect_payloads:
            raise StepFailure("history payloads differ")
        if step.expect_cursor_none is not None:
            is_none = page.cursor is None
            if is_none != step.expect_cursor_none:
                raise StepFailure(
                    f"cursor None={is_none}, expected {step.expect_cursor_none}"
                )
        return

    if isinstance(step, Search):
        page = store.search(
            step.stream, dict(step.filters), cursor=step.cursor, limit=step.limit
        )
        ids = tuple(item.aggregate_id for item in page.items)
        if ids != step.expect_ids:
            raise StepFailure(f"search ids {ids} != {step.expect_ids}")
        if step.expect_cursor_none is not None:
            is_none = page.cursor is None
            if is_none != step.expect_cursor_none:
                raise StepFailure("search cursor mismatch")
        return

    if isinstance(step, Claim):
        claimed = list(store.claim(step.queue, limit=step.limit, lease=step.lease))
        ctx.last_claimed = claimed
        if step.expect_none:
            if claimed:
                raise StepFailure(
                    f"claim returned {len(claimed)} intents, expected none"
                )
            return
        got = {(c.subscription_id, c.revision_id, c.attempts) for c in claimed}
        if got != set(step.expect):
            raise StepFailure(f"claimed {got} != expected {set(step.expect)}")
        if any(not isinstance(c.intent_id, UUID) for c in claimed):  # type: ignore[reportUnnecessaryIsInstance]
            raise StepFailure("intent_id is not a UUID")
        return

    if isinstance(step, Settle):
        claimed = ctx.last_claimed
        if not claimed:
            raise StepFailure("settle with nothing claimed")
        available_at = step.available_at
        if step.status == "retry" and available_at is None:
            available_at = datetime.now(UTC) + timedelta(minutes=1)
        settlements = [
            Settlement(
                intent_id=c.intent_id,
                status=step.status,  # type: ignore[arg-type]
                available_at=available_at,
                error=step.error,
            )
            for c in claimed
        ]
        store.settle(settlements)
        return

    if isinstance(step, Wait):
        time.sleep(step.seconds)
        return

    if isinstance(step, Time):
        _run_time(store, ctx, step)
        return

    if isinstance(step, Race):
        _run_race(store, step)
        return

    if isinstance(step, ConcurrentDrainers):
        _run_concurrent_drainers(store, step)
        return

    raise StepFailure(f"unknown step op {type(step).__name__}")


def _run_time(store: Store, ctx: _Context, step: Time) -> None:
    page = store.history(
        AggregateKey(step.stream, step.aggregate_id),
        after=step.after,
        limit=step.limit,
    )
    stamps = [item.committed_at for item in page.items]
    if not stamps:
        raise StepFailure("no revisions to inspect for committed_at")
    for stamp in stamps:
        offset = getattr(stamp, "utcoffset", lambda: None)()
        if offset is None or offset.total_seconds() != 0:
            raise StepFailure(f"committed_at is not tz-aware UTC: {stamp!r}")
    from itertools import pairwise

    for earlier, later in pairwise(stamps):
        if later < earlier:
            raise StepFailure(
                f"committed_at decreased across revisions: {earlier!r} > {later!r}"
            )
    if len(ctx.last_commit_stamps) > 1:
        raise StepFailure(
            "requests inside one commit got different committed_at values: "
            f"{sorted(ctx.last_commit_stamps)!r}"
        )


def _run_race(store: Store, step: Race) -> None:
    from eventic.jsonx import canonical_bytes, digest

    barrier = threading.Barrier(step.writers)
    results: list[str] = []
    lock = threading.Lock()

    def writer(i: int) -> None:
        barrier.wait()
        payload = canonical_bytes({"racer": i})
        request = CommitRequest(
            stream=step.stream,
            aggregate_id=step.aggregate_id,
            expected_revision=step.expected_revision,
            kind=step.kind,  # type: ignore[arg-type]
            schema_version=step.schema_version,
            payload=payload,
            digest=digest(payload),
            meta=canonical_bytes({}),
            meta_version=step.meta_version,
            fingerprint=step.fingerprint,
        )
        try:
            store.commit([request])
            outcome = "ok"
        except RevisionConflict:
            outcome = "conflict"
        except Exception:  # noqa: BLE001
            outcome = "other"
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(step.writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    if any(t.is_alive() for t in threads):
        raise StepFailure("a race writer hung")
    ok = results.count("ok")
    conflict = results.count("conflict")
    other = [r for r in results if r not in ("ok", "conflict")]
    if other:
        raise StepFailure(f"race produced non-Conflict outcomes: {other}")
    if ok != 1:
        raise StepFailure(f"race had {ok} winners, expected exactly 1")
    if conflict != step.writers - 1:
        raise StepFailure(f"race had {conflict} conflicts, expected {step.writers - 1}")


def _run_concurrent_drainers(store: Store, step: ConcurrentDrainers) -> None:
    claimed_sets: list[set[UUID]] = []
    lock = threading.Lock()

    def drainer() -> None:
        claimed = list(store.claim(step.queue, limit=step.limit, lease=step.lease))
        with lock:
            claimed_sets.append({c.intent_id for c in claimed})

    threads = [threading.Thread(target=drainer) for _ in range(step.drainers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    if any(t.is_alive() for t in threads):
        raise StepFailure("a drainer hung")
    union: set[UUID] = set()
    for claimed in claimed_sets:
        union |= claimed
    total = sum(len(s) for s in claimed_sets)
    if total != len(union):
        raise StepFailure(
            f"an intent was claimed by two drainers ({total} claims for "
            f"{len(union)} distinct intents)"
        )
    if len(union) != step.expect_total:
        raise StepFailure(
            f"drainers claimed {len(union)} of {step.expect_total} intents"
        )


def run_scenario(
    factory: Callable[[], Store],
    scenario: Scenario,
    *,
    _fresh: bool = True,
) -> ScenarioResult:
    try:
        stores = [factory() for _ in range(max(1, scenario.stores))]
    except Exception as exc:  # noqa: BLE001
        return ScenarioResult(
            scenario.name,
            failures=[f"store factory failed: {type(exc).__name__}: {exc}"],
        )
    store = stores[0]
    caps = _capability_names(store.capabilities)
    if not scenario.requires <= caps:
        return ScenarioResult(
            scenario.name,
            skipped_reason=f"requires capabilities {sorted(scenario.requires - caps)}",
        )
    ctx = _Context()
    failures: list[str] = []
    for index, step in enumerate(scenario.steps):
        try:
            _run_step(store, ctx, step)
        except StepFailure as exc:
            failures.append(f"step {index} [{step.name}]: {exc}")
        except EventicError as exc:
            failures.append(
                f"step {index} [{step.name}]: raised {type(exc).__name__}: {exc}"
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(
                f"step {index} [{step.name}]: unexpected {type(exc).__name__}: {exc}"
            )
        if failures and _fresh:
            break
    return ScenarioResult(scenario.name, failures=failures)


def run_all(
    factory: Callable[[], Store],
    scenarios: Sequence[Scenario] | None = None,
) -> list[ScenarioResult]:
    corpus = scenarios if scenarios is not None else scenario_data.SCENARIOS
    return [run_scenario(factory, scenario) for scenario in corpus]


def summary(results: Sequence[ScenarioResult]) -> str:
    passed = sum(1 for r in results if r.passed)
    skipped = sum(1 for r in results if r.skipped)
    failed = len(results) - passed - skipped
    lines = [f"{passed} passed, {skipped} skipped, {failed} failed"]
    for result in results:
        for failure in result.failures:
            lines.append(f"  {result.name}: {failure}")
    return "\n".join(lines)


def pause_for_lease(lease: timedelta) -> None:
    """Sleep past a lease so the next claim can reclaim it."""
    time.sleep(lease.total_seconds() + 0.05)
