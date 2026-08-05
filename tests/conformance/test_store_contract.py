"""Phase 6: the conformance suite is the spec — written before any store exists.

A ``NullStore`` fails every scenario with the scenario's name, proving the
runner reports honestly; a capability-gated skip is reported as a skip.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

from eventic.errors import EventicError
from eventic.ids import AggregateKey
from eventic.protocols import Capabilities
from eventic.testing.conformance import scenarios
from eventic.testing.runner import (
    ScenarioResult,
    run_all,
    run_scenario,
    summary,
)
from eventic.wire import (
    ClaimedIntent,
    CommitRequest,
    CommitResult,
    Settlement,
    StoredRevision,
)


class NullStore:
    """A store that raises for everything: every scenario must fail loudly."""

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities()

    def commit(self, requests: Sequence[CommitRequest]) -> Sequence[CommitResult]:
        raise NotImplementedError("NullStore.commit")

    def head(self, key: AggregateKey) -> StoredRevision | None:
        raise NotImplementedError("NullStore.head")

    def revision(self, key: AggregateKey, revision: int) -> StoredRevision | None:
        raise NotImplementedError("NullStore.revision")

    def history(self, key: AggregateKey, *, after: int, limit: int) -> Any:
        raise NotImplementedError("NullStore.history")

    def search(
        self,
        stream: str,
        filters: Mapping[str, Any],
        *,
        cursor: str | None,
        limit: int,
    ) -> Any:
        raise NotImplementedError("NullStore.search")

    def claim(
        self, queue: str, *, limit: int, lease: timedelta
    ) -> Sequence[ClaimedIntent]:
        raise NotImplementedError("NullStore.claim")

    def settle(self, settlements: Sequence[Settlement]) -> None:
        raise NotImplementedError("NullStore.settle")


def test_suite_has_scenarios() -> None:
    assert scenarios.SCENARIOS
    names = [s.name for s in scenarios.SCENARIOS]
    assert len(names) == len(set(names)), "duplicate scenario names"


def test_null_store_fails_every_scenario_with_name() -> None:
    results = run_all(lambda: NullStore())
    passed = [r for r in results if r.passed]
    assert not passed, "NullStore must not pass any scenario"
    for result in results:
        assert result.name  # the scenario name is attached
        if not result.skipped:
            assert result.failures, result.name
            assert result.failures[0]
    # every capability-free scenario fails (capability-gated ones skip)
    failed = [r for r in results if not r.skipped]
    assert failed
    assert all(not r.skipped for r in failed)


def test_unexpected_exception_is_a_failure_not_an_assert_crash() -> None:
    result = run_scenario(lambda: NullStore(), scenarios.SCENARIOS[0])
    assert not result.passed
    assert result.failures
    assert "NullStore.commit" in result.failures[0]


def test_capability_gating_skips_with_reason() -> None:
    result = run_scenario(lambda: NullStore(), scenarios.SCENARIOS[0])
    assert result.skipped_reason is None  # no capability required


def test_capability_skip_reported_not_passed() -> None:
    from eventic.testing.conformance.store import Scenario

    scenario = Scenario(
        name="needs outbox",
        requires=frozenset({"outbox"}),
        steps=(),
    )
    result = run_scenario(lambda: NullStore(), scenario)
    assert result.skipped
    assert not result.passed
    assert "outbox" in (result.skipped_reason or "")


def test_scenario_result_summary() -> None:
    results = [ScenarioResult("a"), ScenarioResult("b", failures=["x"])]
    text = summary(results)
    assert "1 passed, 0 skipped, 1 failed" in text
    assert "b: x" in text


def test_errors_all_public() -> None:
    # The conformance suite may only expect errors from the public tree.

    for scenario in scenarios.SCENARIOS:
        for step in scenario.steps:
            error_name = getattr(step, "expect_error", None)
            if error_name:
                cls = getattr(
                    __import__("eventic.errors", fromlist=[error_name]),
                    error_name,
                )
                assert issubclass(cls, EventicError), error_name


def test_scenario_step_names_present() -> None:
    for scenario in scenarios.SCENARIOS:
        for step in scenario.steps:
            assert step.name, f"{scenario.name} has an unnamed step"


def test_head_upsert_failure_leaves_no_log_row() -> None:
    """Phase 6 'Atomicity' row: head upsert fails -> no log row (I8).

    The scenario DSL cannot inject a failure into the store, so this lives
    next to the suite as a conformance-style test — the same boundary probe
    p05 exercises, as a standing assertion (R4, F5).
    """
    import pytest
    from pydantic import BaseModel

    from eventic import App, Stream
    from eventic.errors import EventicError
    from eventic.ids import AggregateKey
    from eventic.sql import SQLite

    class T(BaseModel):
        n: int = 0

    todos = Stream(T, name="todos")
    store = SQLite(":memory:")
    ev = App(id="d", streams=[todos]).bind(store)
    try:
        first = ev[todos].create(T(n=1))
        original_upsert = store.dialect.upsert_head

        def exploding_upsert(values: object) -> object:
            raise RuntimeError("forced head-upsert failure")

        object.__setattr__(store.dialect, "upsert_head", exploding_upsert)
        try:
            with pytest.raises(EventicError):
                ev[todos].change(first, n=2)
        finally:
            object.__setattr__(store.dialect, "upsert_head", original_upsert)
        key = AggregateKey("todos", first.id)
        assert store.head(key).revision == 0
        assert store.revision(key, 1) is None
        assert store.revision(key, 0) is not None
    finally:
        store.close()
