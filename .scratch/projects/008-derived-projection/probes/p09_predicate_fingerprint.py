"""Spike 9 (REVIEW C1): can a predicate be fingerprinted?

CONCEPT SS7 asks for a real guarantee:

    "a pattern whose predicate changed without a version bump is a declaration
     error, caught by ``eventic schema check``, not a silent divergence"

and SS4/SS4.1 assume the predicate is a plain callable (``when=became(...)``,
``correlate=lambda c: ...``).  Stream fingerprints work because a pydantic
schema is a *value* (``canonical.model_fingerprint``).  A callable is not.

This matters for PHASING, not just for SS7.  SS11 sells Phase 1 as "roughly a
day and worth doing regardless" -- but Phase 1 fixes the public ``Predicate``
type, and SS7 lives in Phase 5.  If Phase 1 ships raw callables, SS7 is
foreclosed permanently.  That makes the "safe, independent" first phase a
one-way door.

Part 1 shows the three ways callable fingerprinting fails.
Part 2 implements the alternative -- a small frozen combinator algebra -- and
shows it is hashable, serializable, diffable, and evaluable at BOTH tiers from
one definition (the SS4.1 unification, done properly).

Run: devenv shell -- uv run python .scratch/projects/008-derived-projection/probes/p09_predicate_fingerprint.py
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping, Union

# ---------------------------------------------------------------------------
# The shared predicate input (SPIKES F3.1: not the full Commit)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PredicateView:
    """Plan-time-decidable parts of a commit; built by the planner from
    (base, planned state) and by the matcher from a hydrated Commit."""

    stream: str
    kind: Literal["create", "change"]
    changed: frozenset[str]
    state: Mapping[str, Any]
    meta: Mapping[str, Any]


# ---------------------------------------------------------------------------
# PART 1 -- the callable form, and why SS7 cannot be built on it
# ---------------------------------------------------------------------------

THRESHOLD = 100  # a module-level global, captured by predicate C


def became_callable(key: str, value: object) -> Callable[[PredicateView], bool]:
    return lambda view: key in view.changed and view.state.get(key) == value


def over_threshold_callable() -> Callable[[PredicateView], bool]:
    # Captures a GLOBAL, not a closure variable.
    return lambda view: int(view.state.get("amount", 0)) > THRESHOLD


def code_fingerprint(fn: Callable[..., Any]) -> str:
    return hashlib.sha256(fn.__code__.co_code).hexdigest()[:16]


def probe_callable_failures() -> None:
    print("  F1. Bytecode is BLIND to the values that define the semantics.")
    failed = became_callable("status", "failed")
    cancelled = became_callable("status", "cancelled")
    print(f"      became('status','failed')    -> {code_fingerprint(failed)}")
    print(f"      became('status','cancelled') -> {code_fingerprint(cancelled)}")
    print(
        f"      same code object: {failed.__code__ is cancelled.__code__}  "
        f"same fingerprint: {code_fingerprint(failed) == code_fingerprint(cancelled)}"
    )
    assert code_fingerprint(failed) == code_fingerprint(cancelled)
    print(
        "      => two semantically DIFFERENT predicates fingerprint identically.\n"
        "         SS7's check would pass a real semantic change. Silent divergence,\n"
        "         which is the exact thing SS7 exists to prevent."
    )

    print("\n  F2. Reading closure cells helps -- until the capture is a global.")
    cells_failed = tuple(c.cell_contents for c in (failed.__closure__ or ()))
    cells_cancelled = tuple(c.cell_contents for c in (cancelled.__closure__ or ()))
    print(f"      closure of became('status','failed'):    {cells_failed}")
    print(f"      closure of became('status','cancelled'): {cells_cancelled}")
    print("      -> distinguishable. Now the global-capturing predicate:")
    global THRESHOLD
    over = over_threshold_callable()
    before_fp, before_cells = code_fingerprint(over), over.__closure__
    THRESHOLD = 999  # a real, deploy-visible semantic change
    after = over_threshold_callable()
    print(f"      THRESHOLD 100 -> 999")
    print(f"      fingerprint before={before_fp} after={code_fingerprint(after)}")
    print(f"      closure before={before_cells} after={after.__closure__}")
    assert code_fingerprint(over) == code_fingerprint(after)
    assert not before_cells and not after.__closure__
    print(
        "      => the predicate's meaning changed completely; bytecode identical,\n"
        "         closure empty. NOTHING observable changed. SS7 cannot be built\n"
        "         on callables."
    )
    THRESHOLD = 100

    print("\n  F3. Bytecode is also brittle in the other direction (false positives).")
    a = lambda view: view.state.get("status") == "failed"  # noqa: E731
    b = lambda view: (lambda s: s == "failed")(view.state.get("status"))  # noqa: E731
    print(f"      two spellings of ONE predicate: {code_fingerprint(a)} vs {code_fingerprint(b)}")
    assert code_fingerprint(a) != code_fingerprint(b)
    print(
        "      => a pure refactor trips the check. A fingerprint with both false\n"
        "         negatives (F1/F2) and false positives (F3) is not a gate."
    )


# ---------------------------------------------------------------------------
# PART 2 -- the combinator algebra: a predicate that is a VALUE
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Became:
    key: str
    value: object

    def evaluate(self, view: PredicateView) -> bool:
        return self.key in view.changed and view.state.get(self.key) == self.value

    def to_json(self) -> dict[str, Any]:
        return {"op": "became", "key": self.key, "value": self.value}


@dataclass(frozen=True, slots=True)
class Equals:
    key: str
    value: object

    def evaluate(self, view: PredicateView) -> bool:
        return view.state.get(self.key) == self.value

    def to_json(self) -> dict[str, Any]:
        return {"op": "equals", "key": self.key, "value": self.value}


@dataclass(frozen=True, slots=True)
class AtLeast:
    key: str
    value: float

    def evaluate(self, view: PredicateView) -> bool:
        raw = view.state.get(self.key)
        return isinstance(raw, (int, float)) and raw >= self.value

    def to_json(self) -> dict[str, Any]:
        return {"op": "at_least", "key": self.key, "value": self.value}


@dataclass(frozen=True, slots=True)
class And:
    terms: tuple["Predicate", ...]

    def evaluate(self, view: PredicateView) -> bool:
        return all(t.evaluate(view) for t in self.terms)

    def to_json(self) -> dict[str, Any]:
        return {"op": "and", "terms": [t.to_json() for t in self.terms]}


@dataclass(frozen=True, slots=True)
class Or:
    terms: tuple["Predicate", ...]

    def evaluate(self, view: PredicateView) -> bool:
        return any(t.evaluate(view) for t in self.terms)

    def to_json(self) -> dict[str, Any]:
        return {"op": "or", "terms": [t.to_json() for t in self.terms]}


Predicate = Union[Became, Equals, AtLeast, And, Or]


def predicate_fingerprint(p: Predicate) -> str:
    """Canonical, stable, and a pure function of the predicate's MEANING."""
    canonical = json.dumps(p.to_json(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def probe_combinator() -> None:
    failed = Became("status", "failed")
    cancelled = Became("status", "cancelled")
    print(f"  Became('status','failed')    -> {predicate_fingerprint(failed)}")
    print(f"  Became('status','cancelled') -> {predicate_fingerprint(cancelled)}")
    assert predicate_fingerprint(failed) != predicate_fingerprint(cancelled)
    print("  distinct fingerprints for distinct meanings (fixes F1)")

    big = And((Became("status", "failed"), AtLeast("amount", 100)))
    big2 = And((Became("status", "failed"), AtLeast("amount", 999)))
    print(f"\n  And(became, at_least(100)) -> {predicate_fingerprint(big)}")
    print(f"  And(became, at_least(999)) -> {predicate_fingerprint(big2)}")
    assert predicate_fingerprint(big) != predicate_fingerprint(big2)
    print("  a threshold change is visible (fixes F2)")

    same_again = And((Became("status", "failed"), AtLeast("amount", 100)))
    assert predicate_fingerprint(big) == predicate_fingerprint(same_again)
    assert big == same_again and hash(big) == hash(same_again)
    print("  re-declaring the same predicate is stable & hashable (fixes F3)")

    print(f"\n  serializable for the ledger and for `projection status`:")
    print(f"    {json.dumps(big.to_json(), sort_keys=True)}")

    # -- one definition, two evaluation sites (the SS4.1 unification) --------
    print("\n  ONE definition evaluated at BOTH tiers:")
    plan_view = PredicateView(
        stream="orders",
        kind="change",
        changed=frozenset({"status"}),
        state={"status": "failed", "amount": 250},
        meta={},
    )
    matcher_view = PredicateView(
        stream="orders",
        kind="change",
        changed=frozenset({"status"}),
        state={"status": "failed", "amount": 250},
        meta={},
    )
    assert big.evaluate(plan_view) is True
    assert big.evaluate(matcher_view) is True
    miss = PredicateView(
        stream="orders",
        kind="change",
        changed=frozenset({"amount"}),  # status did not change
        state={"status": "failed", "amount": 250},
        meta={},
    )
    assert big.evaluate(miss) is False
    print(
        "    plan-time view  -> True\n"
        "    matcher view    -> True\n"
        "    status unchanged-> False  (became() is a transition, not a state test)"
    )


def main() -> None:
    print("== PART 1: fingerprinting a callable predicate ==")
    probe_callable_failures()
    print("\n== PART 2: the combinator algebra ==")
    probe_combinator()
    print(
        "\nFinding: SS7's guarantee is unreachable if Phase 1 ships `when=` as a raw\n"
        "callable -- bytecode hashing has both false negatives (closure and global\n"
        "capture) and false positives (refactors). A small frozen combinator algebra\n"
        "is hashable, serializable, ledger-storable, displayable, and evaluable at\n"
        "both tiers from one definition. It costs perhaps half a day MORE in Phase 1\n"
        "and is the difference between SS7 being buildable and being foreclosed.\n"
        "SS11's claim that Phase 1 is safe and independent is therefore false: it is\n"
        "the one-way door in the plan."
    )


if __name__ == "__main__":
    main()
