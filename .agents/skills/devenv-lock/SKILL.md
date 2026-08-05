---
name: devenv-lock
description: Use when a module edit or an input bump doesn't take effect, or to decide which cache layer to reset. The lock vs eval-cache decision tree for devenv repos.
auto_trigger:
  keywords: ["edit didn't take", "nothing changed", "stale lock", "re-lock", "devenv update", "rm -f devenv.lock", "eval cache", "refresh-eval-cache", "lock file", "wrong revision"]
---

# The lock / eval-cache loop

Two cache layers decide whether your edit takes effect, and they reset
differently. Diagnose by asking **what did I edit?**

| What you edited | Layer | Reset |
|---|---|---|
| this repo's `devenv.nix` / `env.*` | **eval cache** (`.devenv/`) | `devenv shell --refresh-eval-cache -- <cmd>`; big hammer: `rm -rf .devenv` |
| a module this repo imports by path (e.g. `repoman`) | **lock** (`devenv.lock`) | `rm -f devenv.lock`; surgically: `devenv update repoman` |
| a remote input upstream | **lock** | `devenv update <input>` / `devenv update` |
| don't know | both | `rm -f devenv.lock && rm -rf .devenv` — slower, but the reliable recovery |

The genome renders the toolchain imports from the answers file, so a generated
`devenv.yaml` carries a path like:

    repoman:
      url: path:{{ repoman_dev_root }}/repoman/modules

That literal `{{ }}` is why `.agents/skills/**` (and the genome's
`.agents/devenv/**`) sit in the template's `_copy_without_render` — Copier must
ship these files byte-for-byte, never render them.

Background: `lock-and-cache.md` and `the-lock-cache-loop.md` in the devenv docs
export. The symptom→cause tree is `devenv-troubleshoot`; "I edited a module and
nothing changed" is `devenv-module-edits`.

For *when* in the lifecycle to rebuild vs. verify vs. commit, see the `repoman` skill.
