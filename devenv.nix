# eventic — repoman-enabled Python devenv.
#
# RepoMan is always on. This Python template is a superset of template-nix: it adds
# the `test` manager (testee — pytest / ruff / ty) to the language-agnostic core
# (copy + git), on top of the Python toolchain.
{ ... }:

{
  repoman.enable = true;
  repoman.managers = [ "copy" "git" "test" ];

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
}
