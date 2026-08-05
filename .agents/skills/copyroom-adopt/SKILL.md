---
name: copyroom-adopt
description: Adopt or templatize an existing repo — bring a hand-written repo under template management with copyroom adopt / templatize, and read the drift report.
auto_trigger:
  keywords: ["adopt a repo", "adopt this repo", "templatize", "extract a template", "bring a repo under management", "template drift report", "copyroom adopt", "copyroom templatize", "answers file", "drift"]
---

# Adopt / templatize an existing repo

CopyRoom brings an existing, hand-written repo — one with no CopyRoom markers —
under template management. Two paths; pick by whether a template exists:

- **You already have a template** → `copyroom adopt <template> --ref <ref> --answers <file> [--write]`
- **No template yet** → `copyroom templatize [--into PATH]` → parameterize
  (`copyroom golden` loop) → finalize (git init + tag) → `copyroom adopt`

The template is **named or extracted, never guessed** — CopyRoom will not
fuzzy-match a registry.

## Rules

- **Adoption is report-only unless `--write`.** The only file it can write into
  the repo is `.copier-answers.yml`. Drift is information, not a problem to
  auto-fix; there is no `--reconcile`.
- **The template source must be a git repository** (a ref must be renderable).
- **`adopt` refuses an already-managed repo** (`.copier-answers.yml` present)
  unless `--force`.
- **Author the `--answers` file yourself** from the template's `copier.yml`;
  `--write` only records the link once the drift report looks right.

The drift report has three parts: *Template adds* (files the template produces
that the repo lacks), *Differs* (divergent content), and *Repo-only* (the repo's
legitimately-extra content). A reviewable patch lands under `.copyroom/adopt/`.

Detail: `docs/user/adoption.md` — read it before running the arc.

For when to adopt vs. scaffold a new project, see the `copyroom` skill.
