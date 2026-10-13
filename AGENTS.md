# AGENTS.md — project instructions

CopyRoom created this repo from the **template-py** source, a RepoMan-managed
Python project. This file is the canonical instructions file — `CLAUDE.md` is a
symlink to it, so every agent tool reads the same source.

## How to work here

- **Run project commands inside the `devenv` shell.** It pins Python and wires RepoMan.
  Never invoke bare `uv`/`python`/`pytest`. The host profile puts the `*man`
  commands (copyroom, gitman, repoman) and `testee` on `PATH`. Run `testee` from
  the repository root, in or out of the shell. It checks the commit in its own workspace
  and starts its own clean devenv shell there.
- **The lifecycle is RepoMan's.** Change → verify → integrate. Birth and convergence (copyroom)
  are activities with no fixed order. For the order and the routing,
  start at the `repoman` skill; for domain detail open the per-tool skills.
- **Exit codes are an API:** `0` ok · `1` act on findings · `2` the tool could not run.
- **Verify before you integrate:** run `testee verify`. Then run `testee status -r REV` on the
  exact revision that you push, for example `@-`. Integrate only on exit 0. Native `jj git push`
  runs no Git hook, so check the verdict yourself. Never push on red. Use native `jj`:
  `jj describe -m "message"`, `jj new`, `jj bookmark create NAME -r @-`,
  `jj git push --bookmark NAME`, and `gh pr create`.
- **Gitman only opens workspaces:** `gitman work NAME`. Native `jj` and `gh` do the rest.
- **Testee commands:**
  - `testee verify` — every required check that has no verdict for the tree. The full gate.
  - `testee verify <id>...` — named checks, such as `ruff` or `pytest`. Verdicts add up.
  - `testee verify --profile quick` — the quick profile (ruff, ruff-format).
  - `testee status -r REV` — is this commit green? It runs nothing.
  - `testee show` — verdicts, failures, and log paths. Add `--json` for the saved records.
  - `testee doctor` — inspect the host and the project setup.
- **Testee has no fix command.** Run the formatter directly:
  `devenv shell -- ruff format src tests`. Then rerun `testee verify`.
- **Checks are Nix data** in `devenv.nix` (`testee.checks`). To add or change a check, edit
  that file. Do not add a separate Testee config file or a `[tool.testee]` table.
  Keep `devenv.lock` and `uv.lock` in Git. A stale `devenv.lock` gives `lock_stale`. A stale
  `uv.lock` gives `prepare_failed`.

## Where things live

- `src/` — the Python package; `tests/` — its tests.
- `devenv.nix` / `devenv.yaml` — the shell, Testee checks, and `eventic:*` tasks.
- `pyproject.toml` — package metadata and the `test`, `migrate`, and `postgres` extras.
  The strict `basedpyright` scope is `src/eventic`.
- `.repoman/project.toml` — the lifecycle manager roster; `.agents/` and `.claude/`
  — machine-local links maintained by the central Devman link plane.

## Staying current

- **Template updates:** the project stays on its pinned template ref. Run `copyroom status`
  to see the pin and the newer version tags. Run `copyroom update --ref NEW_REF` to preview a
  three-way merge in a separate workspace. Review the preview, then run
  `copyroom update --apply PATH`.
- **One RepoMan import only:** if the central Devman overlay already imports the RepoMan
  module, do not also declare the `repoman` input in `devenv.yaml`. devenv fails with
  "option repoman.enable is already declared". The central overlay imports the
  machine-installed module at `/run/current-system/sw/share/repoman/module/devenv.nix`.

## Agent-files convention

- **Skills** are supplied by the central Devman link plane at
  `.agents/skills/<name>/SKILL.md`; do not commit or materialize a second copy
  in this project.
- **Ownership:** `AGENTS.md` is canonical and `CLAUDE.md` is its symlink. The
  `.agents/` and `.claude/` links are machine-local and stay gitignored.
