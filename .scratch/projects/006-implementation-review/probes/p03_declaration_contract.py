"""Spec fidelity of the declaration layer: error taxonomy, equality, R13/R14.

ARCHITECTURE.md §2.1 gives a table mapping each App check to a specific error
class. §8 defines the tree. This probe asks which of those are reachable.
"""

from __future__ import annotations

from pydantic import BaseModel

from eventic import App, Stream, Subscription
from eventic.envelopes import Commit
from eventic.errors import (
    ConfigError,
    DuplicateId,
    UnknownStream,
    UnsupportedHandler,
)
from eventic.evolution import make_upcaster
from eventic.meta import Meta


class Todo(BaseModel):
    text: str = ""


class Other(BaseModel):
    value: int = 0


def h(c: Commit[Todo, BaseModel]) -> None: ...


async def ah(c: Commit[Todo, BaseModel]) -> None: ...


print("=== ARCHITECTURE.md §2.1 error-class table ===")
todos = Stream(Todo, name="todos")
dupe = Stream(Todo, name="todos")
elsewhere = Stream(Other, name="elsewhere")

cases: list[tuple[str, type[ConfigError], object]] = [
    ("duplicate stream name", DuplicateId, lambda: App(id="a", streams=[todos, dupe])),
    (
        "duplicate subscription id",
        DuplicateId,
        lambda: App(
            id="a",
            streams=[todos],
            subscriptions=[
                Subscription(id="s", stream=todos, handler=h),
                Subscription(id="s", stream=todos, handler=h),
            ],
        ),
    ),
    (
        "subscription on uninstalled stream",
        UnknownStream,
        lambda: App(
            id="a",
            streams=[todos],
            subscriptions=[Subscription(id="s", stream=elsewhere, handler=h)],
        ),
    ),
    (
        "async handler",
        UnsupportedHandler,
        lambda: App(
            id="a",
            streams=[todos],
            subscriptions=[Subscription(id="s", stream=todos, handler=ah)],
        ),
    ),
]

mismatches = 0
for label, expected, build in cases:
    try:
        build()  # type: ignore[operator]
        actual = "NO ERROR"
    except ConfigError as exc:
        actual = type(exc).__name__
    agree = actual == expected.__name__
    mismatches += 0 if agree else 1
    print(f"  {label:38} spec={expected.__name__:24} actual={actual}  {'ok' if agree else 'MISMATCH'}")
assert mismatches == 0, f"{mismatches} §2.1 taxonomy mismatches (F6)"

print("\n=== R14: Stream / Meta / App equality ===")
a = Stream(Todo, name="todos", schema_version=1)
b = Stream(Todo, name="todos", schema_version=1)
c = Stream(Other, name="todos", schema_version=1)  # different MODEL, same name
print(f"  Stream(Todo,'todos') == Stream(Other,'todos')      -> {a == c}")
print("  (name-only is deliberate and documented: App equality is")
print("   identity-of-declaration, not equivalence-of-behaviour)")
m1 = Meta(Todo, version=1)
m2 = Meta(Todo, version=2, upcasters={1: make_upcaster(1, 2, lambda t: t)})
print(f"  Meta(Todo,version=1) == Meta(Todo,version=2)       -> {m1 == m2}")
print(f"  hash equal                                         -> {hash(m1) == hash(m2)}")
app1 = App(id="a", streams=[a], meta=m1)
app2 = App(id="a", streams=[c], meta=m2)
print(f"  App(streams=[Todo],meta=v1) == App([Other],meta=v2)-> {app1 == app2}")
assert a == c  # deliberate
assert m1 != m2, "F9: Meta equality must include version"
assert app1 != app2

print("\n=== R13: make_upcaster identity ===")
fn = lambda t: t  # noqa: E731
u1 = make_upcaster(1, 2, fn)
u2 = make_upcaster(1, 2, fn)
print(f"  make_upcaster(1,2,fn) == make_upcaster(1,2,fn)     -> {u1 == u2}")
print(f"  same class                                         -> {type(u1) is type(u2)}")
print("  (nothing in the library compares upcasters, so this is latent only)")

print("\n=== F13: changed_keys reports a key removed from `before` ===")
from eventic.planning import changed_keys  # noqa: E402

before = {"a": 1, "removed": 2}
after = {"a": 1}
print(f"  changed_keys({before}, {after}) = {sorted(changed_keys(before, after))}")
print("  -> a deleted top-level key IS reported as changed (F13)")
assert changed_keys(before, after) == frozenset({"removed"})
