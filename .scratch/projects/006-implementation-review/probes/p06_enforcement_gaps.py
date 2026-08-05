"""Are the I4/R9 and I9 enforcement tests actually comprehensive?

§3.3 I4 asks: "do they catch a new module-level dict? a new `import sqlalchemy`
at module level in a leaf?"  §3.7 asks whether the "exactly once" grep misses
the CLI help strings or the examples.

This probe injects the violations those tests are supposed to catch and re-runs
the tests' own logic against the mutated package.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from pathlib import Path

import eventic

ROOT = Path(eventic.__file__).resolve().parent.parent.parent


# --- the test's own logic, lifted verbatim ---------------------------------
def all_modules() -> list[object]:
    modules = [eventic]
    for info in pkgutil.walk_packages(eventic.__path__, eventic.__name__ + "."):
        if info.name == "eventic.sql.migrations" or info.name.startswith(
            "eventic.sql.migrations."
        ):
            continue
        importlib.import_module(info.name)
        modules.append(sys.modules[info.name])
    return modules


def offenders() -> list[str]:
    found: list[str] = []
    for module in all_modules():
        for name, value in vars(module).items():
            if name.startswith("_"):
                continue
            if isinstance(value, (dict, list, set)):
                found.append(f"{module.__name__}.{name}")
    return found


print("=== I4 / R9: test_no_module_level_mutable_binding ===")
print(f"  clean tree offenders: {offenders()}")

import eventic.planning as planning  # noqa: E402

# The exact bug the redesign exists to delete: an ambient process-global cache.
# Written the way a Python developer would actually write it.
planning._CURRENT_STORE = {}  # type: ignore[attr-defined]
planning._SEEN_AGGREGATES = set()  # type: ignore[attr-defined]
print(f"  after injecting planning._CURRENT_STORE = {{}} and _SEEN_AGGREGATES = set():")
print(f"    offenders: {offenders()}")
print("  -> the scan skips every name starting with '_', so a private module-level")
print("     dict/set/list — the conventional spelling for a cache — is invisible.")
assert offenders() == [], "expected the gap: private globals are not scanned"
del planning._CURRENT_STORE  # type: ignore[attr-defined]
del planning._SEEN_AGGREGATES  # type: ignore[attr-defined]

# And a public one IS caught, confirming the scan works for the shape it checks:
planning.CACHE = {}  # type: ignore[attr-defined]
print(f"  after injecting a PUBLIC planning.CACHE = {{}}: {offenders()}")
assert offenders() == ["eventic.planning.CACHE"]
del planning.CACHE  # type: ignore[attr-defined]

print("\n=== I9 / item 15: the 'exactly once' grep scope ===")


def scanned_files() -> list[Path]:
    return sorted(ROOT.joinpath("src").rglob("*.py")) + sorted(
        ROOT.joinpath("docs").rglob("*.md")
    )


scanned = {p.resolve() for p in scanned_files()}
print(f"  files scanned: {len(scanned)}")

docs_like = [
    ROOT / "README.md",
    ROOT / "AGENTS.md",
]
for path in docs_like:
    if path.exists():
        print(f"  {path.name:12} scanned? {path.resolve() in scanned}")

readme = (ROOT / "README.md").read_text().lower()
print(f"  README.md actually contains 'exactly once'? {'exactly once' in readme}")
print("  -> the claim holds today, but README.md — the primary documentation —")
print("     is outside the grep's scope, so a regression there is unguarded.")

print("\n=== R10: worker.run_forever shutdown ===")
import inspect  # noqa: E402

from eventic.worker import Worker  # noqa: E402

src = inspect.getsource(Worker.run_forever)
print("  " + "\n  ".join(src.strip().splitlines()))
has_signal = "signal" in src or "SIGTERM" in src or "stop" in src.lower()
print(f"  any signal/stop handling? {has_signal}")
print("  -> an unbounded `while True` with a blocking sleep and no stop flag;")
print("     the only exit is an exception. A deployed worker cannot drain-and-stop.")
