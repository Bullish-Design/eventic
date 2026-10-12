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

  # Python toolchain. The venv hosts the app and the dev group (pytest, ruff, ty).
  # Run `uv sync` once to fill it. The host profile puts the managers
  # (copyroom, gitman) and `testee` on PATH.
  languages.python = {
    enable = true;
    venv.enable = true;
    uv.enable = true;
  };

  # Keep `uv.lock` and `devenv.lock` in Git. Checks run through `uv run --no-sync`
  # because the prepare step has already made the venv match `uv.lock`.
  testee.prepare = [ [ "uv" "sync" "--locked" ] ];
  testee.checks = {
    ruff = {
      argv = [ "uv" "run" "--no-sync" "ruff" "check" "--output-format" "json" "src" "tests" ];
      profiles = [ "quick" "full" ];
      structured = { parser = "ruff-json"; file = "ruff.stdout.log"; };
    };
    ruff-format = {
      argv = [ "uv" "run" "--no-sync" "ruff" "format" "--check" "src" "tests" ];
      profiles = [ "quick" "full" ];
    };
    ty = {
      argv = [ "uv" "run" "--no-sync" "ty" "check" "src" "tests" ];
      profiles = [ "full" ];
    };
    pytest = {
      argv = [
        "${pkgs.bash}/bin/bash" "-c"
        ''uv run --no-sync pytest tests --junitxml="$TESTEE_RUN_DIR/pytest.junit.xml"''
      ];
      profiles = [ "full" ];
      structured = { parser = "junit-xml"; file = "pytest.junit.xml"; };
    };
  };

  tasks = {
    # Stable Devman base-group entrypoints. Both call `testee verify`. Verdicts
    # add up: after `base:check`, `base:test` runs only the checks with no verdict.
    "base:check".exec = ''
      cd "$DEVENV_ROOT"
      testee verify ruff ruff-format ty
    '';
    "base:test".exec = ''
      cd "$DEVENV_ROOT"
      testee verify
    '';
  };
}
