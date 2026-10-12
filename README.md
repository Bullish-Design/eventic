# eventic

A versioned document store with transactional change notification.

A [repoman](https://github.com/Bullish-Design/repoman)-managed [devenv](https://devenv.sh)
**Python** project. RepoMan is always enabled. The selected lifecycle manager
roster is recorded in `.repoman/project.toml`. [Testee](https://github.com/Bullish-Design/testee)
checks each commit against the checks declared in `devenv.nix`.

## Getting started

```bash
devenv shell          # enter the pinned environment
uv sync --all-extras  # app deps + the dev group (pytest/ruff/ty)
repoman-sync          # check that repoman is on PATH + generate the router skill
repoman managers      # list the wired managers
repoman doctor        # check RepoMan's own wiring
```

## Verify

Install the `testee` command once per machine. See the Testee README for the Nix profile
install. Run these commands from the repository root. Testee checks the commit in its own
workspace, so untracked and ignored files cannot change a verdict.

```bash
testee verify                # every required check with no verdict yet: ruff, ruff-format, ty, pytest
testee verify pytest         # one named check; single checks add up to a full gate
testee verify --profile quick  # ruff + ruff-format only
testee status -r @-          # is this commit green? Runs nothing. Run it before you integrate
testee show                  # verdicts, failures, and log paths
testee doctor                # inspect the host and the project setup
```

Exit codes: `0` green, `1` a check failed, `2` no verdict (never verified, incomplete, or a refusal
with a reason code). Edit the checks in `devenv.nix` under `testee.checks`. `testee.prepare` runs
`uv sync --locked` first, so keep `uv.lock` and `devenv.lock` in Git. Testee has no fix command:
run `ruff format src tests` in the devenv shell, then rerun `testee verify`.

## Version control

The project is a colocated `jj` repository. The host profile provides `jj`, `gh`, and
`gitman`. `gitman work NAME` opens a workspace. Native `jj` and `gh` do the rest:

```bash
jj describe -m "message"                 # describe the change
jj new                                   # start the next change
jj bookmark create NAME -r @-            # name the described change
jj git push --bookmark NAME              # push (runs no hook: run testee status -r @- first)
gh pr create                             # open the pull request
```

## Layout

```
src/eventic/   # package source
tests/                    # pytest tests (the `pytest` Testee check)
.repoman/project.toml     # lifecycle manager roster
.devman/project.toml      # stable project identity for the machine link plane
devenv.nix                # shell, toolchain, and the Testee checks
devenv.yaml               # inputs, including the locked Testee module (devenv.lock pins it)
pyproject.toml            # project metadata + dev group (pytest, ruff, ty) + ruff/pytest config
```

## Develop a *man tool from a local checkout

`devenv.yaml` pins published tags. To use a local checkout of a tool instead, create the
untracked file `devenv.local.yaml` beside `devenv.yaml`. devenv merges it over `devenv.yaml`:

```yaml
inputs:
  repoman:
    url: "path:/path/to/repoman/modules"
    flake: false
```

A shell with the overlay re-locks `devenv.lock` at the local path. Move the overlay away and
run `devenv update` before you commit.

## Staying current with the template

This project was created from the `template-py` source. CopyRoom saves a snapshot of that
source in `.copyroom-local/`. Pull template updates with:

```bash
copyroom update                 # preview a three-way merge in a separate workspace
copyroom update --apply PATH    # apply the preview that you reviewed
```

## Author

Bullish Design <BullishDesignEngineering@gmail.com>
