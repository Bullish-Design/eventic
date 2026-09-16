# eventic — repoman-enabled Python devenv.
#
# RepoMan is always on. This Python template is a superset of template-nix: it adds
# the `test` manager (testee — pytest / ruff / ty) to the language-agnostic core
# (copy + git), on top of the Python toolchain.
{ ... }:

{
  # Project 039: the store-toolchain opt-out moved to the tracked manifests --
  # `cliProvider = "venv"` in .repoman/project.toml, and `[toolchain] enable =
  # false` in vendomat.toml. Both modules now reach this repository from the
  # system profile, so neither option belongs to an input declared here.

  # Python toolchain. The venv hosts the app + testee (the verify manager's tools
  # pytest/ruff/ty run inside this codebase, project 12); the pure-CLI managers
  # (copyroom/gitman) come from the system-wide toolchain venv (`repoman-sync
  # --machine`) instead.
  languages.python = {
    enable = true;
    # Matches pyproject requires-python and the CI matrix leg. Resolves only
    # because devenv.yaml declares the nixpkgs-python input.
    version = "3.13";
    venv.enable = true;
    uv.enable = true;
  };

  # devman — the automation plane (CONCEPT.md §5). `base` alone: this repository
  # ships no scheduled work and writes none of its own files.
  # https://devenv.sh/tasks/
  #
  # The two task names the `base` group calls (groups/base/README.md). devenv
  # owns each implementation; Dagu owns the composition (§6). pytest and ruff
  # live in the `test` extra (not `dev`), so the flag is `--extra test`
  # (STAGE_7_LOG.md, wave 2b). `ruff check src` matches the repo's own scope.
  tasks = {
    "eventic:lint".exec = "uv run --extra test ruff check src";
    "eventic:test".exec = "uv run --extra test pytest";

    "base:check".after = [ "eventic:lint" ];
    "base:test".after = [ "eventic:test" ];
  };
}
