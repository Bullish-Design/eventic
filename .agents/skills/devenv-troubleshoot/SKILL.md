---
name: devenv-troubleshoot
description: Use on "command not found", the shell won't enter, or eval errors. A symptom→cause→fix decision tree for devenv repos.
auto_trigger:
  keywords: ["command not found", "shell won't enter", "eval error", "devenv broken", "won't build", "infinite recursion", "attribute missing", "devenv fails"]
---

# devenv symptom → cause → fix

| Symptom | Likely cause | Fix |
|---|---|---|
| `command not found` for a project CLI | running outside the shell, or deps not synced | `devenv shell -- <cmd>`; sync the venv → `devenv-python-venv` |
| `ModuleNotFoundError` on import | venv exists but deps unsynced | `uv sync` → `devenv-python-venv` |
| `gitman`/`copyroom`/`docman`: command not found | machine toolchain not bootstrapped | `repoman-sync --machine` (repoman checkout) → `command-not-found-in-shell` |
| Edited a module, no effect | stale lock / eval cache | `rm -f devenv.lock && rm -rf .devenv` → `devenv-module-edits` |
| Import of a module fails | missing `flake: false` or transitive input | `devenv-inputs` |
| Python version pin ignored | no `nixpkgs-python` input | `devenv-inputs` |
| Shell won't enter / eval error | bad `.nix` syntax or stale eval cache | re-read the error; `devenv shell --refresh-eval-cache`; for the full reset `rm -rf .devenv` |
| Server ties up the shell | used `scripts` for a daemon | use `processes` + `devenv up` → `devenv-processes` |

Background on how PATH is assembled (venv vs nix vs scripts): `command-not-found-in-shell.md`.

For *when* in the lifecycle to stop and fix vs. proceed, see the `repoman` skill.
