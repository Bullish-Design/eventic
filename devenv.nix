# eventic — RepoMan-managed Python devenv.
#
# `.repoman/project.toml` owns the lifecycle manager roster. Testee checks each
# commit in its own workspace and keeps one verdict per check. `devenv.yaml`
# loads the Testee module from a locked input: `devenv.lock` pins its revision.
# The host `testee` command runs the checks declared below. Before the checks,
# Testee runs `testee.prepare` in the workspace, so the venv follows `uv.lock`.
# The check tools (pytest, ruff, ty) come from the `dev` dependency group in
# pyproject.toml.
{ pkgs, ... }:

{
  repoman.enable = true;

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

  # Match .github/workflows/ci.yml: install the same extras and build the wheel.
  # Keep both lock files in Git. Checks run through uv run --no-sync.
  testee.prepare = [
    [ "uv" "sync" "--locked" "--extra" "test" "--extra" "postgres" "--extra" "migrate" ]
    [ "uv" "build" ]
  ];
  testee.checks = {
    ruff = {
      argv = [ "uv" "run" "--no-sync" "ruff" "check" "--output-format" "json" "." ];
      profiles = [ "quick" "full" ];
      structured = { parser = "ruff-json"; file = "ruff.stdout.log"; };
    };
    ruff-format = {
      argv = [ "uv" "run" "--no-sync" "ruff" "format" "--check" "." ];
      profiles = [ "quick" "full" ];
    };
    basedpyright = {
      argv = [ "uv" "run" "--no-sync" "basedpyright" ];
      profiles = [ "full" ];
    };
    pytest = {
      argv = [
        "${pkgs.bash}/bin/bash" "-c"
        ''uv run --no-sync pytest -W error --junitxml="$TESTEE_RUN_DIR/pytest.junit.xml"''
      ];
      profiles = [ "full" ];
      structured = { parser = "junit-xml"; file = "pytest.junit.xml"; };
    };
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
