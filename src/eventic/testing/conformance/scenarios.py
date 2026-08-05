"""The store conformance corpus: scenarios, declaratively.

Grouped by contract area: CAS, replay, identity, atomicity, batch, reads,
head integrity, time, intents, and error translation.
"""

from __future__ import annotations

from uuid import UUID

from eventic.jsonx import canonical_bytes, digest
from eventic.testing.conformance.store import (
    Batch,
    Claim,
    Commit,
    ConcurrentDrainers,
    History,
    Payload,
    Race,
    Scenario,
    Search,
    Settle,
    Time,
    Wait,
    commit_step,
    exact_step,
    head_step,
    intent,
)

_A = UUID(int=1)
_B = UUID(int=2)

_DOC1: Payload = {"text": "a", "done": False}


def _rid(stream: str, aid: UUID, revision: int) -> UUID:
    from eventic.ids import revision_id as rid

    return rid(stream, aid, revision)


_DOC2: Payload = {"text": "b", "done": False}
_DOC3: Payload = {"text": "c", "done": True}


def _commit(
    stream: str,
    aid: UUID,
    expected: int | None,
    kind: str,
    obj: Payload,
    **kw: object,
) -> Commit:
    return commit_step(stream, aid, expected, kind, obj, **kw)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Compare-and-swap
# ---------------------------------------------------------------------------

CAS: tuple[Scenario, ...] = (
    Scenario(
        "create on empty aggregate",
        steps=(
            _commit("todos", _A, None, "create", _DOC1, expect_revision=0),
            head_step("todos", _A, expect_revision=0, expect_payload=_DOC1),
        ),
    ),
    Scenario(
        "create when head exists conflicts",
        steps=(
            _commit("todos", _A, None, "create", _DOC1),
            _commit(
                "todos", _A, None, "create", _DOC2, expect_error="RevisionConflict"
            ),
        ),
    ),
    Scenario(
        "change with correct expected revision",
        steps=(
            _commit("todos", _A, None, "create", _DOC1),
            _commit("todos", _A, 0, "change", _DOC2, expect_revision=1),
            head_step("todos", _A, expect_revision=1, expect_payload=_DOC2),
        ),
    ),
    Scenario(
        "change with stale expected revision conflicts",
        steps=(
            _commit("todos", _A, None, "create", _DOC1),
            _commit("todos", _A, 0, "change", _DOC2),
            _commit("todos", _A, 0, "change", _DOC3, expect_error="RevisionConflict"),
            head_step("todos", _A, expect_revision=1, expect_payload=_DOC2),
        ),
    ),
    Scenario(
        "change with ahead expected revision conflicts",
        steps=(
            _commit("todos", _A, None, "create", _DOC1),
            _commit("todos", _A, 5, "change", _DOC2, expect_error="RevisionConflict"),
        ),
    ),
    Scenario(
        "change with negative expected revision conflicts",
        steps=(
            _commit("todos", _A, None, "create", _DOC1),
            _commit("todos", _A, -1, "change", _DOC2, expect_error="RevisionConflict"),
        ),
    ),
    Scenario(
        "change on nonexistent aggregate conflicts",
        steps=(
            _commit("todos", _A, 0, "change", _DOC1, expect_error="RevisionConflict"),
            head_step("todos", _A, expect_missing=True),
        ),
    ),
)

# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

REPLAY: tuple[Scenario, ...] = (
    Scenario(
        "byte-identical replay is a silent no-op, one row",
        steps=(
            _commit("todos", _A, None, "create", _DOC1),
            _commit(
                "todos",
                _A,
                None,
                "create",
                _DOC1,
                expect_replayed=True,
                expect_revision=0,
            ),
            _commit("todos", _A, 0, "change", _DOC2),
            _commit(
                "todos", _A, 0, "change", _DOC2, expect_replayed=True, expect_revision=1
            ),
            History(
                name="history has exactly two rows",
                stream="todos",
                aggregate_id=_A,
                expect_revisions=(0, 1),
            ),
        ),
    ),
    Scenario(
        "replay with different digest conflicts",
        steps=(
            _commit("todos", _A, None, "create", _DOC1),
            _commit(
                "todos", _A, None, "create", _DOC2, expect_error="RevisionConflict"
            ),
        ),
    ),
    Scenario(
        "replay with different meta conflicts",
        steps=(
            commit_step(
                "todos",
                _A,
                None,
                "create",
                _DOC1,
                meta={"tag": "x"},
            ),
            commit_step(
                "todos",
                _A,
                None,
                "create",
                _DOC1,
                meta={"tag": "y"},
                expect_error="RevisionConflict",
            ),
        ),
    ),
    Scenario(
        "replay with different schema version conflicts",
        steps=(
            commit_step("todos", _A, None, "create", _DOC1, schema_version=1),
            commit_step(
                "todos",
                _A,
                None,
                "create",
                _DOC1,
                schema_version=2,
                expect_error="RevisionConflict",
            ),
        ),
    ),
    Scenario(
        "replay of a superseded revision leaves the head alone",
        steps=(
            _commit("todos", _A, None, "create", _DOC1),
            _commit("todos", _A, 0, "change", _DOC2),
            _commit("todos", _A, 1, "change", _DOC3),
            _commit(
                "todos",
                _A,
                0,
                "change",
                _DOC2,
                expect_replayed=True,
                expect_revision=1,
            ),
            head_step(
                "todos",
                _A,
                expect_revision=2,
                expect_digest=digest(canonical_bytes(_DOC3)),
            ),
        ),
    ),
)

# ---------------------------------------------------------------------------
# Identity: the same UUID in two streams
# ---------------------------------------------------------------------------

IDENTITY: tuple[Scenario, ...] = (
    Scenario(
        "same aggregate UUID in two streams is two aggregates",
        steps=(
            _commit("todos", _A, None, "create", _DOC1),
            _commit("audits", _A, None, "create", _DOC2),
            head_step("todos", _A, expect_revision=0, expect_payload=_DOC1),
            head_step("audits", _A, expect_revision=0, expect_payload=_DOC2),
            _commit("todos", _A, 0, "change", _DOC3, expect_revision=1),
            exact_step("audits", _A, 0, expect_payload=_DOC2),
            head_step("audits", _A, expect_revision=0),
        ),
    ),
)

# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------

ATOMICITY: tuple[Scenario, ...] = (
    Scenario(
        "invalid intent aborts the whole commit",
        steps=(
            _commit(
                "todos",
                _A,
                None,
                "create",
                _DOC1,
                intents=(intent("sub", UUID(int=99), queue=""),),
                expect_error="StoreError",
            ),
            head_step("todos", _A, expect_missing=True),
            History(
                name="log is empty",
                stream="todos",
                aggregate_id=_A,
                expect_revisions=(),
            ),
        ),
    ),
    Scenario(
        "batch with a mid-batch conflict writes nothing",
        steps=(
            Batch(
                name="batch of three with a conflict in the middle",
                commits=(
                    Commit(
                        name="create A",
                        stream="todos",
                        aggregate_id=_A,
                        expected_revision=None,
                        kind="create",
                        schema_version=1,
                        payload=canonical_bytes(_DOC1),
                        digest=digest(canonical_bytes(_DOC1)),
                        meta=canonical_bytes({}),
                        meta_version=1,
                        expect_revision=0,
                    ),
                    Commit(
                        name="change B without a create conflicts",
                        stream="todos",
                        aggregate_id=_B,
                        expected_revision=0,
                        kind="change",
                        schema_version=1,
                        payload=canonical_bytes(_DOC2),
                        digest=digest(canonical_bytes(_DOC2)),
                        meta=canonical_bytes({}),
                        meta_version=1,
                    ),
                    Commit(
                        name="create B would succeed alone",
                        stream="todos",
                        aggregate_id=_B,
                        expected_revision=None,
                        kind="create",
                        schema_version=1,
                        payload=canonical_bytes(_DOC3),
                        digest=digest(canonical_bytes(_DOC3)),
                        meta=canonical_bytes({}),
                        meta_version=1,
                    ),
                ),
                expect_error="RevisionConflict",
            ),
            head_step("todos", _A, expect_missing=True),
            head_step("todos", _B, expect_missing=True),
        ),
    ),
)

# ---------------------------------------------------------------------------
# Batch ordering
# ---------------------------------------------------------------------------

BATCH: tuple[Scenario, ...] = (
    Scenario(
        "two writes to the same aggregate chain in one batch",
        steps=(
            Batch(
                name="create then change chain",
                commits=(
                    Commit(
                        name="create",
                        stream="todos",
                        aggregate_id=_A,
                        expected_revision=None,
                        kind="create",
                        schema_version=1,
                        payload=canonical_bytes(_DOC1),
                        digest=digest(canonical_bytes(_DOC1)),
                        meta=canonical_bytes({}),
                        meta_version=1,
                        expect_revision=0,
                    ),
                    Commit(
                        name="chained change",
                        stream="todos",
                        aggregate_id=_A,
                        expected_revision=0,
                        kind="change",
                        schema_version=1,
                        payload=canonical_bytes(_DOC2),
                        digest=digest(canonical_bytes(_DOC2)),
                        meta=canonical_bytes({}),
                        meta_version=1,
                        expect_revision=1,
                    ),
                ),
            ),
            History(
                name="both rows present",
                stream="todos",
                aggregate_id=_A,
                expect_revisions=(0, 1),
                expect_payloads=(_DOC1, _DOC2),
            ),
        ),
    ),
)

# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

READS: tuple[Scenario, ...] = (
    Scenario(
        "head, exact revisions, and history after several writes",
        steps=(
            _commit("todos", _A, None, "create", _DOC1),
            _commit("todos", _A, 0, "change", _DOC2),
            _commit("todos", _A, 1, "change", _DOC3),
            head_step("todos", _A, expect_revision=2, expect_payload=_DOC3),
            exact_step("todos", _A, 0, expect_payload=_DOC1),
            exact_step("todos", _A, 1, expect_payload=_DOC2),
            exact_step("todos", _A, 2, expect_payload=_DOC3),
            exact_step("todos", _A, 3, expect_missing=True),
            History(
                name="full history in order",
                stream="todos",
                aggregate_id=_A,
                expect_revisions=(0, 1, 2),
                expect_payloads=(_DOC1, _DOC2, _DOC3),
                expect_cursor_none=True,
            ),
            History(
                name="history after 0",
                stream="todos",
                aggregate_id=_A,
                after=0,
                expect_revisions=(1, 2),
            ),
        ),
    ),
    Scenario(
        "history paging with cursors",
        steps=(
            _commit("todos", _A, None, "create", _DOC1),
            _commit("todos", _A, 0, "change", _DOC2),
            _commit("todos", _A, 1, "change", _DOC3),
            History(
                name="first page of one",
                stream="todos",
                aggregate_id=_A,
                limit=1,
                expect_revisions=(0,),
                expect_cursor_none=False,
            ),
        ),
    ),
    Scenario(
        "history on a missing aggregate is empty",
        steps=(
            History(
                name="missing history",
                stream="todos",
                aggregate_id=_A,
                expect_revisions=(),
                expect_cursor_none=True,
            ),
        ),
    ),
    Scenario(
        "search equality on top-level and dotted paths",
        requires=frozenset({"json_paths"}),
        steps=(
            _commit("todos", _A, None, "create", {"text": "a", "meta": {"tag": "x"}}),
            _commit("todos", _B, None, "create", {"text": "b", "meta": {"tag": "y"}}),
            Search(
                name="top-level equality",
                stream="todos",
                filters={"text": "a"},
                expect_ids=(_A,),
            ),
            Search(
                name="dotted path equality",
                stream="todos",
                filters={"meta.tag": "x"},
                expect_ids=(_A,),
            ),
        ),
    ),
    Scenario(
        "missing path and explicit JSON null are distinct",
        requires=frozenset({"json_paths"}),
        steps=(
            _commit("todos", _A, None, "create", {"text": "a", "meta": None}),
            _commit("todos", _B, None, "create", {"text": "b"}),
            Search(
                name="null matches null",
                stream="todos",
                filters={"meta": None},
                expect_ids=(_A,),
            ),
            Search(
                name="null does not match missing",
                stream="todos",
                filters={"text": "b", "meta": None},
                expect_ids=(),
            ),
        ),
    ),
)

# ---------------------------------------------------------------------------
# Head integrity and time
# ---------------------------------------------------------------------------

HEAD_TIME: tuple[Scenario, ...] = (
    Scenario(
        "head digest equals log digest at every revision",
        steps=(
            _commit("todos", _A, None, "create", _DOC1),
            head_step("todos", _A, expect_digest=digest(canonical_bytes(_DOC1))),
            _commit("todos", _A, 0, "change", _DOC2),
            head_step("todos", _A, expect_digest=digest(canonical_bytes(_DOC2))),
        ),
    ),
    Scenario(
        "committed_at is UTC, non-decreasing, and equal within a batch",
        steps=(
            Batch(
                name="two requests in one commit",
                commits=(
                    Commit(
                        name="create",
                        stream="todos",
                        aggregate_id=_A,
                        expected_revision=None,
                        kind="create",
                        schema_version=1,
                        payload=canonical_bytes(_DOC1),
                        digest=digest(canonical_bytes(_DOC1)),
                        meta=canonical_bytes({}),
                        meta_version=1,
                        expect_revision=0,
                    ),
                    Commit(
                        name="chained change",
                        stream="todos",
                        aggregate_id=_A,
                        expected_revision=0,
                        kind="change",
                        schema_version=1,
                        payload=canonical_bytes(_DOC2),
                        digest=digest(canonical_bytes(_DOC2)),
                        meta=canonical_bytes({}),
                        meta_version=1,
                        expect_revision=1,
                    ),
                ),
            ),
            Time(
                name="timestamps are UTC and non-decreasing",
                stream="todos",
                aggregate_id=_A,
            ),
        ),
    ),
)

# ---------------------------------------------------------------------------
# Intents and delivery
# ---------------------------------------------------------------------------

INTENTS: tuple[Scenario, ...] = (
    Scenario(
        "one function under two subscriptions produces two intent rows",
        requires=frozenset({"outbox"}),
        steps=(
            _commit(
                "todos",
                _A,
                None,
                "create",
                _DOC1,
                intents=(
                    intent("sub.a", _rid("todos", _A, 0)),
                    intent("sub.b", _rid("todos", _A, 0)),
                ),
            ),
            Claim(
                name="both intents claimed",
                queue="q",
                expect=(
                    ("sub.a", _rid("todos", _A, 0), 1),
                    ("sub.b", _rid("todos", _A, 0), 1),
                ),
            ),
        ),
    ),
    Scenario(
        "claim, deliver, ack deletes the intent",
        requires=frozenset({"outbox"}),
        steps=(
            _commit(
                "todos",
                _A,
                None,
                "create",
                _DOC1,
                intents=(intent("sub.a", _rid("todos", _A, 0)),),
            ),
            Claim(
                name="claim one",
                queue="q",
                expect=(("sub.a", _rid("todos", _A, 0), 1),),
            ),
            Settle(name="ack delivered", status="delivered"),
            Claim(name="queue is empty", queue="q", expect_none=True),
        ),
    ),
    Scenario(
        "retry makes the intent available again after backoff",
        requires=frozenset({"outbox"}),
        steps=(
            _commit(
                "todos",
                _A,
                None,
                "create",
                _DOC1,
                intents=(intent("sub.a", _rid("todos", _A, 0)),),
            ),
            Claim(
                name="claim one",
                queue="q",
                expect=(("sub.a", _rid("todos", _A, 0), 1),),
            ),
            Settle(name="nack with retry", status="retry", error="boom"),
            Claim(name="reclaim after retry", queue="q", expect_none=True),
        ),
    ),
    Scenario(
        "dead-lettered intent is not claimable",
        requires=frozenset({"outbox"}),
        steps=(
            _commit(
                "todos",
                _A,
                None,
                "create",
                _DOC1,
                intents=(intent("sub.a", _rid("todos", _A, 0)),),
            ),
            Claim(
                name="claim one",
                queue="q",
                expect=(("sub.a", _rid("todos", _A, 0), 1),),
            ),
            Settle(name="dead-letter", status="dead", error="boom"),
            Claim(name="queue is empty", queue="q", expect_none=True),
        ),
    ),
    Scenario(
        "expired lease is reclaimable",
        requires=frozenset({"outbox"}),
        steps=(
            _commit(
                "todos",
                _A,
                None,
                "create",
                _DOC1,
                intents=(intent("sub.a", _rid("todos", _A, 0)),),
            ),
            Claim(
                name="claim with a tiny lease",
                queue="q",
                lease=__import__("datetime").timedelta(milliseconds=50),
                expect=(("sub.a", _rid("todos", _A, 0), 1),),
            ),
            Wait(name="lease expires", seconds=0.1),
            Claim(
                name="reclaim after lease expiry",
                queue="q",
                expect=(("sub.a", _rid("todos", _A, 0), 2),),
            ),
        ),
    ),
)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

ERRORS: tuple[Scenario, ...] = (
    Scenario(
        "constraint violations surface as StoreError, never a driver exception",
        steps=(
            Commit(
                name="empty stream name",
                stream="",
                aggregate_id=_A,
                expected_revision=None,
                kind="create",
                schema_version=1,
                payload=canonical_bytes(_DOC1),
                digest=digest(canonical_bytes(_DOC1)),
                meta=canonical_bytes({}),
                meta_version=1,
                expect_error="StoreError",
            ),
            Commit(
                name="negative revision",
                stream="todos",
                aggregate_id=_A,
                expected_revision=-2,
                kind="change",
                schema_version=1,
                payload=canonical_bytes(_DOC1),
                digest=digest(canonical_bytes(_DOC1)),
                meta=canonical_bytes({}),
                meta_version=1,
                expect_error="RevisionConflict",
            ),
        ),
    ),
)

# ---------------------------------------------------------------------------
# Concurrency (I7): lost races are loud on both backends
# ---------------------------------------------------------------------------

CONCURRENCY: tuple[Scenario, ...] = (
    Scenario(
        "same-expected-revision race has exactly one winner",
        steps=(
            _commit("todos", _A, None, "create", _DOC1),
            Race(
                name="eight writers race one revision",
                stream="todos",
                aggregate_id=_A,
                expected_revision=0,
                writers=8,
            ),
            head_step("todos", _A, expect_revision=1),
        ),
    ),
    Scenario(
        "concurrent create of the same aggregate has exactly one winner",
        steps=(
            Race(
                name="eight writers race one create",
                stream="todos",
                aggregate_id=_A,
                expected_revision=None,
                kind="create",
                writers=8,
            ),
            head_step("todos", _A, expect_revision=0),
        ),
    ),
    Scenario(
        "concurrent drainers claim each intent without overlap",
        requires=frozenset({"concurrent_drainers"}),
        steps=(
            _commit(
                "todos",
                _A,
                None,
                "create",
                _DOC1,
                intents=(
                    intent("sub.a", _rid("todos", _A, 0)),
                    intent("sub.b", _rid("todos", _A, 0)),
                ),
            ),
            ConcurrentDrainers(
                name="two drainers split two intents",
                queue="q",
                drainers=2,
                limit=1,
                expect_total=2,
            ),
        ),
    ),
)

SCENARIOS: tuple[Scenario, ...] = (
    *CAS,
    *REPLAY,
    *IDENTITY,
    *ATOMICITY,
    *BATCH,
    *READS,
    *HEAD_TIME,
    *INTENTS,
    *ERRORS,
    *CONCURRENCY,
)
