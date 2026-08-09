"""Spike 4 (CONCEPT phase 4, SS5.2): re-emitting a match is a no-op.

The concept's central mechanism claim: the matcher can crash after emitting
but before checkpointing, and on restart re-emits. Because the match's
aggregate id is deterministic -- ``uuid5(NS, f"{pattern}:{version}:{key}:{terminal}")``
-- the duplicate emission lands on the existing replay path
(``_commit_one``): ``_is_identical`` matches, the store returns
``replayed=True`` and returns *before the intent insert loop*. So a duplicated
emission writes nothing and wakes no subscriber (SS5.2, I14).

Under test, against the REAL store, no new machinery:

  1. A deterministic re-emit produces exactly one log row and exactly one
     intent row (I14: re-emitting a match is a no-op).
  2. A different terminal revision (a genuinely new match) emits a NEW row --
     the deterministic id is per-terminal, not a global dedup key.
  3. The "wakes no subscriber" claim, checked precisely: the outbox path
     wakes no subscriber (no new intent). The INLINE path DOES re-run the
     handler on replay -- ``Collection._materialize`` dispatches inline
     regardless of ``result.replayed``. That is a pre-existing property of
     replays in general, but it means the concept's claim is outbox-scoped as
     written; inline subscribers on an emit stream are at-least-once and must
     be idempotent, exactly like every other inline/outbox handler.

Run: devenv shell -- uv run python .scratch/projects/008-derived-projection/probes/p04_reemit_idempotent.py
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid5

from pydantic import BaseModel
from sqlalchemy import func, select

from eventic import App, Stream, Subscription
from eventic.envelopes import Commit
from eventic.ids import NS
from eventic.planning import plan_create
from eventic.sql import SQLite
from eventic.sql.tables import eventic_intent, eventic_revision
from eventic.subscription import Outbox


class RevisionRef(BaseModel):
    model_config = {"frozen": True}

    stream: str
    aggregate_id: UUID
    revision: int


class Match(BaseModel):
    model_config = {"frozen": True}

    pattern_id: str
    pattern_version: int
    correlation_key: str
    steps: tuple[RevisionRef, ...]


def match_id(pattern_id: str, version: int, key: str, terminal: UUID) -> UUID:
    return uuid5(NS, f"{pattern_id}:{version}:{key}:{terminal}")


class Todo(BaseModel):
    text: str


def main() -> None:
    fraud_alerts = Stream(Match, name="fraud.alerts.v1")
    app = App(
        id="spike4",
        streams=[fraud_alerts],
        subscriptions=[
            Subscription(
                id="fraud.worker.v1",
                stream=fraud_alerts,
                handler=lambda c: None,  # type: ignore[arg-type]
                delivery=Outbox(queue="q"),
            ),
        ],
    )
    ev = app.bind(SQLite(":memory:"))

    pattern_id = "fraud.velocity.v1"
    key = "acct-1"
    terminal = UUID(int=7)
    match = Match(
        pattern_id=pattern_id,
        pattern_version=1,
        correlation_key=key,
        steps=(RevisionRef(stream="orders", aggregate_id=UUID(int=1), revision=0),),
    )
    rid = match_id(pattern_id, 1, key, terminal)

    print("== first emission ==")
    request = plan_create(app, fraud_alerts, match, rid)
    result = ev.store.commit([request])[0]
    print(f"  replayed={result.replayed} revision={result.revision}")
    assert result.replayed is False

    def row_counts() -> tuple[int, int]:
        with ev.store.engine.connect() as conn:
            log = conn.execute(select(func.count()).select_from(eventic_revision)).scalar_one()
            intents = conn.execute(select(func.count()).select_from(eventic_intent)).scalar_one()
        return log, intents

    log_rows, intent_rows = row_counts()
    print(f"  log rows={log_rows} intent rows={intent_rows}")
    assert (log_rows, intent_rows) == (1, 1)

    print("\n== duplicate emission (crash before checkpoint, restart) ==")
    request2 = plan_create(app, fraud_alerts, match, rid)
    result2 = ev.store.commit([request2])[0]
    print(f"  replayed={result2.replayed}")
    assert result2.replayed is True, "duplicate emission must be absorbed as a replay"
    log_rows, intent_rows = row_counts()
    print(f"  log rows={log_rows} intent rows={intent_rows}")
    assert (log_rows, intent_rows) == (1, 1), (
        "the replay must write no log row and no new intent"
    )
    print("  OK: log and intents unchanged -- the duplicate emission is a no-op.")

    print("\n== a genuinely new match (different terminal) emits a new row ==")
    terminal2 = UUID(int=8)
    match2 = Match(
        pattern_id=pattern_id,
        pattern_version=1,
        correlation_key=key,
        steps=(RevisionRef(stream="orders", aggregate_id=UUID(int=1), revision=1),),
    )
    rid2 = match_id(pattern_id, 1, key, terminal2)
    result3 = ev.store.commit([plan_create(app, fraud_alerts, match2, rid2)])[0]
    log_rows, intent_rows = row_counts()
    print(f"  replayed={result3.replayed} log rows={log_rows} intent rows={intent_rows}")
    assert result3.replayed is False and log_rows == 2 and intent_rows == 2

    print("\n== the 'wakes no subscriber' claim, checked precisely ==")
    inline_calls: list[str] = []

    def inline_handler(c: Commit[Match, object]) -> None:
        inline_calls.append("woken")

    app_inline = App(
        id="spike4b",
        streams=[fraud_alerts],
        subscriptions=[
            Subscription(
                id="fraud.inline.v1",
                stream=fraud_alerts,
                handler=inline_handler,  # type: ignore[arg-type]
            ),
        ],
    )
    ev_inline = app_inline.bind(SQLite(":memory:"))
    ev_inline[fraud_alerts].create(match, id=rid)  # type: ignore[arg-type]
    first_calls = len(inline_calls)
    # Simulate the matcher's crash-before-checkpoint re-emission via the
    # ordinary runtime path (the same call the matcher makes):
    ev_inline[fraud_alerts].create(match, id=rid)  # type: ignore[arg-type]
    print(
        f"  outbox: no new intent -> subscriber not woken (verified above).\n"
        f"  inline: handler ran {first_calls} time(s) on first emit, "
        f"{len(inline_calls)} after the duplicate re-emit."
    )
    if len(inline_calls) == 2:
        print(
            "  Finding: inline dispatch re-runs on replay -- "
            "`_materialize` dispatches regardless of result.replayed. The "
            "concept's 'wakes no subscriber' holds for outbox (the intent is "
            "the wake); inline subscribers on an emit stream are at-least-once "
            "and must be idempotent, as every handler already is. Optionally "
            "add a replayed guard in dispatch_inline -- a behavior change to "
            "all streams, so it needs its own test."
        )
    else:
        print("  (inline did NOT re-run -- behavior differs from expectation)")

    print("\nOK: I14 (re-emitting a match is a no-op) holds on the real store;")


if __name__ == "__main__":
    main()
