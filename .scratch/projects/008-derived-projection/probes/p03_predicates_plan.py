"""Spike 3 (CONCEPT phase 1): predicates, decidable at plan time.

The concept's claims under test:

  1. SS4.1: "a predicate is a pure function of a Commit, and where you place
     it decides whether it is evaluated transactionally or in the projection"
     -- i.e. ONE predicate type shared by ``Subscription.when`` (tier 1) and
     ``Step.when`` (tier 2).
  2. SS4.1: the changed-key computation "must move forward into the planning
     path. _plan_change already holds base, so the information is there -- it
     is a consolidation, not new machinery."
  3. Phase 1 gate: "new tests prove no intent row is written for a filtered
     commit."

The wrinkle the spike has to resolve: a full ``Commit`` is NOT constructible
at plan time. ``Revision`` requires ``committed_at`` (the database clock,
only known after COMMIT), and the predicate the concept sketches --
``became("status", "failed")`` over ``Commit.changed`` plus the new state --
needs exactly the parts that ARE known at plan time. So the shared predicate
input cannot be the full ``Commit``; it must be a narrower view built in both
contexts from the same logical commit.

Findings this probe establishes:
  F3.1  ``PredicateView(kind, changed, state, meta)`` is the shared input.
        The planner builds it from (base, planned state); the matcher builds
        it from a hydrated ``Commit``. ``became()`` works unchanged in both.
  F3.2  The changed-key computation moves into planning without new machinery:
        ``_plan_change`` already holds ``base``; ``state_tree`` on
        ``base.state`` vs the planned state yields the same set the runtime
        computes today after commit. The two agree (envelope equality, F3).
  F3.3  Filtering in ``intents_for`` is sufficient for the gate: a filtered
        subscription produces no ``IntentRequest``, so the store writes no
        intent row; an unfiltered sibling still gets its intent. The inline
        path applies the same predicate.

Run: devenv shell -- uv run python .scratch/projects/008-derived-projection/probes/p03_predicates_plan.py
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar
from uuid import uuid4

from pydantic import BaseModel

from eventic import App, Stream, Subscription
from eventic.envelopes import Commit, Kind
from eventic.planning import changed_keys, plan_change, plan_create, state_tree
from eventic.sql import SQLite
from eventic.stream import Stream as StreamT

T = TypeVar("T", bound=BaseModel)


# ---------------------------------------------------------------------------
# The shared predicate input (F3.1). A frozen view of the parts of a Commit
# that are decidable at plan time -- and, identically, from a hydrated Commit
# in the matcher.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PredicateView(Generic[T]):
    stream: str
    kind: Kind
    changed: frozenset[str]
    state: T
    meta: object


Predicate = Callable[[PredicateView[Any]], bool]


def became(key: str, value: object) -> Predicate:
    """The concept's helper: the key changed AND the new value equals.

    One definition, two evaluation sites -- the unification SS4.1 asks for.
    """

    def predicate(view: PredicateView[Any]) -> bool:
        return key in view.changed and getattr(view.state, key) == value

    return predicate


# ---------------------------------------------------------------------------
# The planner-side view builder (F3.2): consolidation, not new machinery.
# plan_change/plan_create already hold the base; the probe builds the view
# exactly as Phase 1 would inside those functions.
# ---------------------------------------------------------------------------


def plan_view(
    stream: StreamT[T], base: T | None, new_state: T, kind: Kind, meta: object
) -> PredicateView[T]:
    before = state_tree(stream, base) if base is not None else None
    after = state_tree(stream, new_state)
    return PredicateView(
        stream=stream.name,
        kind=kind,
        changed=changed_keys(before, after),
        state=new_state,
        meta=meta,
    )


def matcher_view(commit: Commit[Any, Any]) -> PredicateView[Any]:
    """The matcher-side builder: from a hydrated Commit, same view type."""
    return PredicateView(
        stream=commit.revision.stream,
        kind=commit.kind,
        changed=commit.changed,
        state=commit.revision.state,
        meta=commit.revision.meta,
    )


# ---------------------------------------------------------------------------
# The Phase-1 filter, applied to the intents the planner already computed
# (F3.3). In the real implementation this lives inside intents_for and the
# predicate rides on the Subscription; here it is the same decision, driven
# by a side map so the probe does not need to change the frozen
# Subscription/App types.
# ---------------------------------------------------------------------------


def filtered_intents(
    app: App,
    request_intents: tuple[object, ...],
    view: PredicateView[Any],
    whens: dict[str, Predicate],
) -> tuple[object, ...]:
    out: list[object] = []
    for intent in request_intents:
        predicate = whens.get(intent.subscription_id)
        if predicate is not None and not predicate(view):
            continue
        out.append(intent)
    return tuple(out)


class Order(BaseModel):
    status: str = "pending"
    account_id: str = ""


orders = Stream(Order, name="orders")


def main() -> None:
    print("== F3.1: one predicate, two evaluation sites ==")
    became_failed = became("status", "failed")

    app = App(
        id="spike3",
        streams=[orders],
        subscriptions=[
            Subscription(
                id="on.failed.v1",
                stream=orders,
                handler=lambda c: None,  # type: ignore[arg-type]
            ),
            Subscription(
                id="on.everything.v1",
                stream=orders,
                handler=lambda c: None,  # type: ignore[arg-type]
            ),
        ],
    )
    whens: dict[str, Predicate] = {"on.failed.v1": became_failed}
    ev = app.bind(SQLite(":memory:"))

    # Plan-time view for a create that IS failed.
    created = plan_create(app, orders, Order(status="failed", account_id="a1"), uuid4())
    create_state = Order.model_validate_json(created.payload)
    v_create = plan_view(orders, None, create_state, "create", {})
    print(f"  planner view (create failed):   became={became_failed(v_create)}")
    assert became_failed(v_create), "became() must fire on a failed create"

    # A change pending -> failed: plan-time view from the base.
    base_rev = ev[orders].create(Order(status="pending", account_id="a1"))
    planned = plan_change(app, orders, base_rev, {"status": "failed"})
    new_state = Order.model_validate_json(planned.payload)
    v_change = plan_view(orders, base_rev.state, new_state, "change", base_rev.meta)
    print(
        f"  planner view (pending->failed):  became={became_failed(v_change)} "
        f"changed={sorted(v_change.changed)}"
    )
    assert became_failed(v_change)

    # The matcher-side view for the SAME logical commit must agree (F3.2).
    r = ev[orders].change(base_rev, status="failed")
    from eventic.hydration import hydrate
    from eventic.ids import AggregateKey

    stored = ev.store.revision(AggregateKey("orders", r.id), r.revision)
    assert stored is not None
    rev = hydrate(orders, app.meta, stored)
    # committed_at differs by design (DB clock); the view does not carry it.
    matcher_commit = Commit[Any, Any](
        kind=planned.kind,
        revision=rev,
        changed=changed_keys(
            state_tree(orders, base_rev.state), state_tree(orders, new_state)
        ),
    )
    v_matcher = matcher_view(matcher_commit)
    print(
        f"  matcher view (same commit):      became={became_failed(v_matcher)} "
        f"changed={sorted(v_matcher.changed)}"
    )
    assert became_failed(v_matcher) == became_failed(v_change)
    assert v_matcher.changed == v_change.changed, "plan and matcher changed disagree"

    print("\n== F3.3: no intent row for a filtered commit ==")
    # Outbox variant of the same subscriptions.
    from eventic.subscription import Outbox

    app_out = App(
        id="spike3b",
        streams=[orders],
        subscriptions=[
            Subscription(
                id="on.failed.v1",
                stream=orders,
                handler=lambda c: None,  # type: ignore[arg-type]
                delivery=Outbox(queue="q"),
            ),
            Subscription(
                id="on.everything.v1",
                stream=orders,
                handler=lambda c: None,  # type: ignore[arg-type]
                delivery=Outbox(queue="q"),
            ),
        ],
    )
    ev_out = app_out.bind(SQLite(":memory:"))
    # A change that does NOT become failed (only account_id changes): the
    # predicate must filter on.failed.v1's intent out, keep the sibling's.
    base_rev_out = ev_out[orders].create(Order(status="pending", account_id="a1"))
    request = plan_change(app_out, orders, base_rev_out, {"account_id": "a9"})
    new_state2 = Order.model_validate_json(request.payload)
    view = plan_view(orders, base_rev_out.state, new_state2, "change", base_rev_out.meta)
    print(f"  filter view: changed={sorted(view.changed)} became_failed={became_failed(view)}")
    assert not became_failed(view)
    intents = filtered_intents(app_out, request.intents, view, whens)
    filtered_ids = {i.subscription_id for i in request.intents} - {
        i.subscription_id for i in intents
    }
    print(f"  intent rows planned: {sorted(i.subscription_id for i in intents)}")
    print(f"  filtered out:        {sorted(filtered_ids)}")
    assert filtered_ids == {"on.failed.v1"}, filtered_ids
    assert {i.subscription_id for i in intents} == {"on.everything.v1"}

    # Commit the FILTERED request through the real store and count rows.
    from sqlalchemy import func, select

    from eventic.sql.tables import eventic_intent

    results = ev_out.store.commit([dataclasses.replace(request, intents=intents)])
    assert results[0].replayed is False
    rid = results[0].revision_id
    with ev_out.store.engine.connect() as conn:
        count = conn.execute(
            select(func.count()).select_from(eventic_intent).where(
                eventic_intent.c.revision_id == rid
            )
        ).scalar_one()
        rows = conn.execute(
            select(eventic_intent.c.subscription_id).where(
                eventic_intent.c.revision_id == rid
            )
        ).all()
    print(f"  intent rows for this revision: {count} -> {sorted(r[0] for r in rows)}")
    assert count == 1 and rows[0][0] == "on.everything.v1", (count, rows)
    print("  OK: a filtered commit writes NO intent row; the unfiltered sibling does.")

    print("\nOK: one predicate type serves Subscription.when and Step.when;")
    print("    changed_keys moves into planning as a consolidation (F3.2);")
    print("    the Phase 1 gate (no intent row for a filtered commit) holds.")


if __name__ == "__main__":
    main()
