"""Regression probe: the enforcement test sees private module-level mutables.

F4 (006 review): `test_no_global_state.py` skipped every attribute whose name
started with `_`, so injecting `planning._CURRENT_STORE = {}` — the literal
shape of 003/F8's `_ENGINE` and 004/F16's `_CURRENT` — left the suite green.

Fixed (007 Phase 4): the scan now exempts only dunder names (interpreter
machinery) and `model_config` (pydantic's declarative slot). Any other
module-level dict/list/set — public or private — is an offender, and
class-level mutable defaults on module-defined classes are scanned too.
`eventic.encodings._ENCODING_INSTANCES`, a module-level backing dict for the
encoding registry, was removed: the registry is now a `MappingProxyType` over
an inline literal, the only module-level binding being the proxy itself.

Run: devenv shell -- uv run python .scratch/.../probes/p06_enforcement_gaps.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

import eventic  # noqa: E402

from tests.architecture.test_no_global_state import _module_mutables  # noqa: E402

print("=== I4 / R9: private module-level mutables are now offenders ===")
clean = _module_mutables()
print(f"  clean tree offenders: {clean}")
assert clean == [], clean

import eventic.planning as planning  # noqa: E402

# The exact bug the redesign exists to delete: an ambient process-global
# cache, written the way a Python developer would actually write it.
planning._CURRENT_STORE = {}  # type: ignore[attr-defined]
planning._SEEN_AGGREGATES = set()  # type: ignore[attr-defined]
try:
    offenders = _module_mutables()
    print(f"  after injecting planning._CURRENT_STORE = {{}} and _SEEN_AGGREGATES = set():")
    print(f"    offenders: {offenders}")
    assert "eventic.planning._CURRENT_STORE" in offenders, offenders
    assert "eventic.planning._SEEN_AGGREGATES" in offenders, offenders
finally:
    del planning._CURRENT_STORE  # type: ignore[attr-defined]
    del planning._SEEN_AGGREGATES  # type: ignore[attr-defined]

# And a public one is still caught, confirming the scan kept its teeth:
planning.CACHE = {}  # type: ignore[attr-defined]
try:
    offenders = _module_mutables()
    print(f"  after injecting a PUBLIC planning.CACHE = {{}}: {offenders}")
    assert "eventic.planning.CACHE" in offenders, offenders
finally:
    del planning.CACHE  # type: ignore[attr-defined]

# The encoding registry no longer hides a mutable backing dict behind a proxy:
from eventic.encodings import ENCODINGS  # noqa: E402
from types import MappingProxyType  # noqa: E402

assert isinstance(ENCODINGS, MappingProxyType)
print("\nOK: the scan reports injected private mutables;")
print("    ENCODINGS is a MappingProxyType with no mutable backing name.")

print("\n=== I9 / item 15: the 'exactly once' grep scope ===")
from pathlib import Path  # noqa: E402

from tests.architecture.test_delivery_contract import _source_files

scanned = {p.resolve() for p in _source_files()}
print(f"  files scanned: {len(scanned)}")
for name in ("README.md", "AGENTS.md"):
    path = Path(__file__).resolve().parent.parent.parent.parent / name
    if path.exists():
        print(f"  {name:12} scanned? {path.resolve() in scanned}")

print("\n=== R10: worker.run_forever shutdown ===")
import inspect  # noqa: E402

from eventic.worker import Worker  # noqa: E402

src = inspect.getsource(Worker.run_forever)
print("  " + "\n  ".join(src.strip().splitlines()))
has_stop = "stop" in src or "Event" in src
print(f"  stop flag / Event? {has_stop}")
