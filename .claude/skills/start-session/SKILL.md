---
name: start-session
description: Orient before feature or fix work in GamGUI — check the tree and GAM pin, get a green baseline, and read the domain runbook for the area you'll touch. Use at the start of any task that will change code; skip for questions and trivial doc edits. The session-start hook injects a short git + GAM-pin digest automatically.
---

# Start-session — ORIENT

Orient before the first edit, not instead of it. The goal is to load the right
context and catch a stale checkout. Scale it to the task: a question or a
one-line doc fix needs none of this; a feature or a fix in `core/` needs all of
it.

## 1. Where is the tree?

- `git log --oneline -8` and `git status` — what shipped recently, what's dirty.
  (The hook's digest already has the branch, last commit and dirty count.)
- Branching: this is a solo repo and the operator commits straight to `main`
  when they ask for a commit. Use a branch/PR when they ask for one, and always
  for an [improve-rules](../improve-rules/SKILL.md) proposal.

## 2. Is the toolchain sane?

- `.venv/bin/python -m pytest -q` — a green baseline before you change anything,
  so a later red is yours (~25s on an idle Mac, ~45s under load). `make setup`
  if there's no `.venv`; it needs Python 3.10+ and the system `python3` is older.
- **GAM pin drift** (the recurring gotcha): a `git pull` moves
  `EXPECTED_GAM_VERSION` in `core/gam/commands.py` but **not** the gitignored
  vendored binary. If the digest reports DRIFT or a missing grammar, re-vendor:
  `make gam` then `.venv/bin/python scripts/build_command_catalog.py`. See
  [docs/domains/build-packaging.md](../../../docs/domains/build-packaging.md).

## 3. Load the domain, not the whole map

CLAUDE.md is always loaded — the constitution. The area-specific knowledge is
**not**: read the [docs/domains](../../../docs/domains) runbook(s) for the area
you're about to touch (index: [docs/domains/README.md](../../../docs/domains/README.md)),
including its failure history. If the area has entries in
[docs/failure-log.md](../../../docs/failure-log.md), read those too.

## 4. Live priorities

- [ROADMAP.md](../../../ROADMAP.md) — the ranked backlog and deliberate
  trade-offs.
- `docs/plans/` holds handoff plans. Each opens with a **Status** line; never
  re-apply a plan, or an item of one, that its Status marks APPLIED — its
  snippets no longer match the code.
- If the rules-improver hook prints a nudge, mention it to the operator and move
  on; [improve-rules](../improve-rules/SKILL.md) is a separate observer pass,
  not this session's work.

## 5. Plan

Set out the task as a short todo list before the first edit. When it's a
mutation to a real tenant, the rule of engagement is explicit per-action
permission — plan the throwaway-account verification into the task.
