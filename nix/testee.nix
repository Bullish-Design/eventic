# Testee checks for this project.
#
# The machine (nix-meta) supplies the Testee module at the `machine` path.
# On a host without that module, the checks are skipped and the shell still
# evaluates. This project owns its checks here. It never pins Testee itself.
# Use `lib.optionalAttrs` for the settings. A module-system condition fails
# when the option is absent.
{ lib, options, pkgs, ... }:

let
  machine = /run/current-system/sw/share/testee/module/testee.nix;
in
{
  imports = lib.optional (builtins.pathExists machine) machine;

  config = lib.optionalAttrs (options ? testee) {
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
        # The two four-way property tests take about 90 s each. The full suite
        # is over the 300 s default, so the check gets 900 s.
        timeout_s = 900;
        profiles = [ "full" ];
        structured = { parser = "junit-xml"; file = "pytest.junit.xml"; };
      };
    };
  };
}
