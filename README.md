# eventic

A versioned document store with transactional change notification.

A [repoman](https://github.com/Bullish-Design/repoman)-managed [devenv](https://devenv.sh)
**Python** project. RepoMan is always enabled. The selected lifecycle manager
roster is recorded in `.repoman/project.toml`. [Testee](https://github.com/Bullish-Design/testee)
runs the checks declared in `devenv.nix`.

## Getting started

```bash
devenv shell          # enter the pinned environment
uv sync --all-extras  # app deps + the dev group (testee + pytest/ruff/ty)
repoman-sync          # check that repoman is on PATH + generate the router skill
repoman managers      # list the wired managers
repoman doctor        # check RepoMan's own wiring
```

## Verify

Install the `testee` command once per machine. See the Testee README for the Nix profile
install. Run these commands from the repository root. `testee` starts its own clean devenv shell.

```bash
testee verify          # quick profile: ruff + ruff-format (partial gate)
testee verify --full   # every required check: ruff, ruff-format, ty, pytest (full gate)
testee check pytest    # one named check (partial gate)
testee report          # latest summary (--json for the saved report)
testee doctor          # inspect the project setup
```

Exit codes: `0` all selected checks passed, `1` a check failed, `2` the run was incomplete or the
environment failed. Edit the checks in `devenv.nix` under `testee.checks`. Testee has no fix command:
run `ruff format src tests` in the devenv shell, then rerun `testee verify`.

## Version control

The project is a colocated `jj` repository. The host profile provides `jj`, `gh`, and
`gitman`. `gitman work NAME` opens a workspace. Native `jj` and `gh` do the rest:

```bash
jj describe -m "message"                 # describe the change
jj new                                   # start the next change
jj bookmark create NAME -r @-            # name the described change
jj git push --bookmark NAME              # push (runs no hook: run testee verify --full first)
gh pr create                             # open the pull request
```

## Layout

```
src/eventic/   # package source
tests/                    # pytest tests (the `pytest` Testee check)
.repoman/project.toml     # lifecycle manager roster
.devman/project.toml      # stable project identity for the machine link plane
devenv.nix                # shell, toolchain, and the Testee checks
pyproject.toml            # project metadata + dev group (testee, pytest, ruff, ty) + ruff/pytest config
```

## Staying current with the template

This project was generated from `template-py`. Pull template updates with:

```bash
copyroom update          # three-way merge from the latest template tag
```

## Author

Bullish Design <BullishDesignEngineering@gmail.com>
