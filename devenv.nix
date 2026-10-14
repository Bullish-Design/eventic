# eventic — RepoMan-managed Python devenv.
#
# `.repoman/project.toml` owns the lifecycle manager roster. Testee checks each
# commit in its own workspace and keeps one verdict per check. The machine
# supplies the Testee module. `nix/testee.nix` declares the checks. The host
# `testee` command runs them. Before the checks, Testee runs `testee.prepare`
# in the workspace, so the venv follows `uv.lock`.
# The check tools (pytest, ruff, ty) come from the `dev` dependency group in
# pyproject.toml.
{ pkgs, ... }:

{
  imports = [ ./nix/testee.nix ];

  # Python toolchain. The venv hosts the app and its `test` extra (pytest,
  # ruff, basedpyright). Run `uv sync --extra test` once to fill it.
  languages.python = {
    enable = true;
    # Matches pyproject requires-python and the CI matrix leg. Resolves only
    # because devenv.yaml declares the nixpkgs-python input.
    version = "3.13";
    venv.enable = true;
    uv.enable = true;
  };

  tasks = {
    "eventic:lint".exec = "uv run --extra test ruff check src";
    "eventic:test".exec = "uv run --extra test pytest";

    # Stable Devman base-group entrypoints. The full gate uses Testee verdicts.
    "base:check".exec = ''
      cd "$DEVENV_ROOT"
      testee verify ruff ruff-format
    '';
    "base:test".exec = ''
      cd "$DEVENV_ROOT"
      testee verify
    '';
  };
}
