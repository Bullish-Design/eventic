"""Spike 1 (CONCEPT phase 3, run first): the Match envelope signature.

The concept's own bar (SS10 "Honest limits"): *"Prototype this signature before
building anything else -- if it cannot be made tolerable, that is the finding
that should change the plan."*

What is being judged:
  a. The persisted document: ``Match`` with ``steps: tuple[RevisionRef, ...]``
     (references, not copies -- I3).
  b. ``resolve()``: the ergonomic cost of references, "paid by a resolve()
     helper on the envelope that batches them" (SS5.1).
  c. The delivery shape: who builds the envelope and how the handler reaches
     the store, given I4 (pure declaration) and I5 (no ambient store, no
     ContextVar, no method on the state model).

Findings this probe establishes:
  F1.1  The persisted document is trivially constructible and pydantic-clean.
  F1.2  ``resolve()`` cannot live on the Match document (I5: no method on the
        state model) and cannot be a free function the handler calls (I4: the
        handler is declared before any Runtime exists). The envelope must
        carry a resolver, which means the emit-stream delivery envelope is a
        NEW type (``MatchEnvelope``), not the plain ``Commit`` the concept
        SS5.1 says is delivered. That is a small, contained delta to
        worker.py / dispatch.py / app.py validation -- not "zero new code",
        but zero new delivery *machinery* (no new queues, retries, or
        dead-letter paths).
  F1.3  The resolve() return type is ``tuple[Revision[Any, Any], ...]`` --
        positionally stable, statically weak. Consumers destructure and get
        ``Any`` states: no casts, but no static checking either. The README's
        "no casts, no registry lookup" claim regresses exactly as the concept
        predicts, and only on this surface.

Run: devenv shell -- uv run python .scratch/projects/008-derived-projection/probes/p01_match_envelope.py
"""

from __future__ import annotations

from typing import Any, Protocol, TypeVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from eventic import App, Stream, Subscription
from eventic.ids import AggregateKey
from eventic.sql import SQLite
from eventic.wire import StoredRevision

# ---------------------------------------------------------------------------
# (a) The persisted document -- references, not copies
# ---------------------------------------------------------------------------


class RevisionRef(BaseModel):
    """One immutable pointer into the log: (stream, aggregate_id, revision)."""

    model_config = ConfigDict(frozen=True)

    stream: str
    aggregate_id: UUID
    revision: int


class Match(BaseModel):
    """The persisted match document. Weakly typed by necessity: it spans N
    revisions of possibly different streams/types."""

    model_config = ConfigDict(frozen=True)

    pattern_id: str
    pattern_version: int
    correlation_key: str
    steps: tuple[RevisionRef, ...]


# ---------------------------------------------------------------------------
# (b) The resolver -- the thing that makes references tolerable
# ---------------------------------------------------------------------------


class Resolver(Protocol):
    """A read handle bound to a store, good for exactly one match."""

    def resolve(
        self, refs: tuple[RevisionRef, ...]
    ) -> tuple[StoredRevision, ...]: ...


class _StoreResolver:
    """The real resolver: N single-row reads, one per reference.

    The concept says resolve() "batches them". The Store protocol has no
    batch-read method (and the concept's one new method is ``scan``, not a
    batch read), so this is a loop of ``store.revision`` calls. Batching is
    the envelope-level grouping (N reads in one call), not a new wire method.
    """

    def __init__(self, store: object) -> None:
        self._store = store  # type: ignore[assignment]

    def resolve(
        self, refs: tuple[RevisionRef, ...]
    ) -> tuple[StoredRevision, ...]:
        out: list[StoredRevision] = []
        for ref in refs:
            row = self._store.revision(  # type: ignore[attr-defined]
                AggregateKey(ref.stream, ref.aggregate_id), ref.revision
            )
            assert row is not None, f"dangling reference {ref}"
            out.append(row)
        return tuple(out)


class MatchEnvelope(BaseModel):
    """The delivery envelope for emit streams: the Match document plus a
    resolver bound at delivery time.

    F1.2: this type is the finding. The concept SS5.1 says "Ordinary
    Subscriptions fire on the emit stream" with the existing worker and
    dispatch untouched, and "a resolve() helper on the envelope". But the
    ordinary envelope (``Commit``) carries no store handle -- handlers are
    pure (I4), and I5 forbids ambient stores, ContextVars, and methods on the
    state model. A handler receiving ``Commit[Match, M]`` has no way to reach
    the store. Either the envelope changes, or the emit stream cannot be
    consumed through the ordinary machinery. This is that envelope.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    match: Match
    resolver: Any  # a Resolver; Any keeps pydantic from building an is-instance
    # validator for a Protocol (SkipValidation is the explicit alternative;
    # either way the field is unchecked at the boundary -- F1.2).

    def resolve(self) -> tuple[StoredRevision, ...]:
        return self.resolver.resolve(self.match.steps)


MatchEnvelope.model_rebuild()  # `resolver: Any` under postponed annotations

T = TypeVar("T", bound=BaseModel)


def _try_variadic_generics() -> str:
    """Attempt: generic over the step payload types (``Match[*Ts]``).

    pydantic 2.11 cannot build a schema for a bare ``tuple[*Ts]`` field at
    class-definition time (SchemaError / unknown-type) -- the class does not
    even define. Even if a custom schema hook made it define, ``resolve()``
    could not map ``Ts`` across a tuple of ``Revision``: Python's type
    system has no tuple-map, so the honest return type would stay
    ``tuple[Revision[Any, Any], ...]``. Verdict (F1.3): the variadic adds
    ceremony for no static benefit. The positionally-stable untyped tuple is
    the tolerable shape.
    """
    from typing import TypeVarTuple

    Ts = TypeVarTuple("Ts")

    try:

        class TypedMatchEnvelope[*Ts](BaseModel):  # type: ignore[no-redef]
            model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

            match: Match
            resolver: Resolver

            def resolve(self) -> tuple[StoredRevision, ...]:
                return self.resolver.resolve(self.match.steps)

        _ = TypedMatchEnvelope[Match, Match, Match]  # noqa: F841
        return "built"
    except Exception as exc:  # noqa: BLE001
        return f"does not build: {type(exc).__name__}"


# ---------------------------------------------------------------------------
# (d) A realistic consumer, run against a real store
# ---------------------------------------------------------------------------


class Order(BaseModel):
    status: str
    account_id: str


orders = Stream(Order, name="orders")
fraud_alerts = Stream(Match, name="fraud.alerts.v1")

seen: list[StoredRevision] = []


def on_fraud(envelope: MatchEnvelope) -> None:
    """The consumer the concept owes its ergonomic promise to."""
    steps = envelope.resolve()
    first = steps[0]
    # The caller narrows: `first` is a StoredRevision here (in the real
    # envelope it would be a hydrated Revision[Any, Any]) -- positionally
    # stable, statically weak (F1.3).
    seen.extend(steps)
    print(f"  resolved {len(steps)} steps; first is {first.stream}/{first.revision}")


def main() -> None:
    print("== (a) the persisted document ==")
    refs = (
        RevisionRef(stream="orders", aggregate_id=uuid4(), revision=0),
        RevisionRef(stream="orders", aggregate_id=uuid4(), revision=1),
        RevisionRef(stream="orders", aggregate_id=uuid4(), revision=2),
    )
    match = Match(
        pattern_id="fraud.velocity.v1",
        pattern_version=1,
        correlation_key="acct-1",
        steps=refs,
    )
    print("  match:", match.model_dump())
    assert match.model_dump()["steps"][0]["stream"] == "orders"

    print("== (c) variadic-generics attempt ==")
    print(f"  Match[*Ts] envelope {_try_variadic_generics()}")

    print("== (d) delivery against a real store ==")
    # The App-validation delta (F1.2) is real: a MatchEnvelope handler is
    # rejected today. For the probe, build the runtime without the
    # match-stream subscription and dispatch the envelope manually -- the
    # point here is the envelope mechanics, not the validation change.
    app = App(
        id="spike",
        streams=[orders, fraud_alerts],
    )
    try:
        App(
            id="spike2",
            streams=[orders, fraud_alerts],
            subscriptions=[
                Subscription(
                    id="fraud.alert.v1",
                    stream=fraud_alerts,
                    handler=on_fraud,  # type: ignore[arg-type]
                ),
            ],
        )
        print("  App validation: MatchEnvelope handler ACCEPTED (unexpected)")
    except Exception as exc:  # noqa: BLE001
        print(f"  App validation rejects a MatchEnvelope handler today: {exc}")
    ev = app.bind(SQLite(":memory:"))
    store = ev.store

    # The matcher has already committed the three orders (not our concern
    # here); commit them so the references resolve. Three SEPARATE aggregates,
    # each failed once -- the shape a three-strike pattern matches.
    a, b, c = (uuid4() for _ in range(3))
    ra = ev[orders].create(Order(status="pending", account_id="acct-1"), id=a)
    rb = ev[orders].create(Order(status="pending", account_id="acct-1"), id=b)
    rb = ev[orders].change(rb, status="failed")
    rc = ev[orders].create(Order(status="pending", account_id="acct-1"), id=c)
    rc = ev[orders].change(rc, status="failed")

    refs = (
        RevisionRef(stream="orders", aggregate_id=a, revision=ra.revision),
        RevisionRef(stream="orders", aggregate_id=b, revision=rb.revision),
        RevisionRef(stream="orders", aggregate_id=c, revision=rc.revision),
    )
    match = Match(
        pattern_id="fraud.velocity.v1",
        pattern_version=1,
        correlation_key="acct-1",
        steps=refs,
    )
    envelope = MatchEnvelope(match=match, resolver=_StoreResolver(store))

    print("== consumer runs (this is what a match-stream handler does) ==")
    # Inline path today: dispatch_inline would call on_fraud with the plain
    # Commit; the F1.2 delta is that match-stream subscriptions receive a
    # MatchEnvelope instead. Simulate the delivery:
    on_fraud(envelope)
    assert len(seen) == 3

    print("\nOK: references resolve against a live store; consumer needs no casts.")
    print(
        "F1.2 blast radius (envelope change): dispatch_inline + Worker._reconstruct "
        "gain one branch ('is this stream an emit target of a Pattern?'), and "
        "App._handler_problems accepts MatchEnvelope annotations for those "
        "streams. No new queues/retries/dead-letter paths."
    )


if __name__ == "__main__":
    main()
