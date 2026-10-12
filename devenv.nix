# eventic — RepoMan-managed Python devenv.
#
# `.repoman/project.toml` owns the lifecycle manager roster. Testee runs the
# checks declared below. The host `testee` command starts before devenv and
# runs them in one clean shell. The check tools (pytest, ruff, ty) come from
# the `dev` dependency group in pyproject.toml. When `test` is on the roster,
# RepoMan wires `repoman:test`, `repoman:test:ci`, and `enterTest` to the host
# wrapper.
{ pkgs, config, ... }:

let
  # The Testee module and the wrapper package come from the same pinned tag.
  # The repository is private: use the `git+https` form, not `github:`.
  testeeFlake = builtins.getFlake "git+https://github.com/Bullish-Design/testee?ref=refs/tags/v0.5.0";
  testeePackage = testeeFlake.packages.${pkgs.stdenv.hostPlatform.system}.testee;
  # Checks run in a clean shell with no ambient PATH. Use absolute tool paths.
  venvBin = "${config.devenv.state}/venv/bin";
in

{
  imports = [ testeeFlake.devenvModules.default ];

  repoman.enable = true;

  # Python toolchain. The venv hosts the app and the dev group (pytest, ruff, ty).
  # Run `uv sync` once to fill it. The host profile puts the managers
  # (copyroom, gitman, docman) and `testee` on PATH.
  languages.python = {
    enable = true;
    venv.enable = true;
    uv.enable = true;
  };

  testee.package = testeePackage;
  testee.checks = {
    ruff = {
      argv = [ "${venvBin}/ruff" "check" "--output-format" "json" "src" "tests" ];
      profiles = [ "quick" "full" ];
      structured = { parser = "ruff-json"; file = "ruff.stdout.log"; };
    };
    ruff-format = {
      argv = [ "${venvBin}/ruff" "format" "--check" "src" "tests" ];
      profiles = [ "quick" "full" ];
    };
    ty = {
      argv = [ "${venvBin}/ty" "check" "--python" "${venvBin}/python3" "src" "tests" ];
      profiles = [ "full" ];
    };
    pytest = {
      argv = [
        "${pkgs.bash}/bin/bash" "-c"
        ''${venvBin}/pytest tests --junitxml="$TESTEE_RUN_DIR/pytest.junit.xml"''
      ];
      profiles = [ "full" ];
      structured = { parser = "junit-xml"; file = "pytest.junit.xml"; };
    };
  };

  tasks = {
    # Stable Devman base-group entrypoints. Both call the Testee wrapper.
    "base:check".exec = ''
      cd "$DEVENV_ROOT"
      testee check ruff ruff-format ty
    '';
    "base:test".exec = ''
      cd "$DEVENV_ROOT"
      testee check pytest
    '';
  };
}
