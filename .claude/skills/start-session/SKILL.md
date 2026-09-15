---
name: start-session
description: The ORIENT routine at the start of a GamGUI session — get your bearings before touching code. The session-start hook surfaces the top of this file plus a git + GAM-pin summary automatically.
---

# Start-session — ORIENT

Before any code, orient. The goal is to load the right context and catch a
stale checkout, not to start editing. **No code during ORIENT.**

## 1. Where is the tree?

- `git log --oneline -8` and `git status` — what shipped recently, what's dirty.
- If on `main`, branch before feature work.

## 2. Is the toolchain sane?

- `.venv/bin/python -m pytest -q` — a green baseline before you change anything,
  so a later red is yours. (`make setup` if there's no `.venv`; needs Python
  3.10+ — the system `python3` may be older than the build's.)
- **GAM pin drift** (the recurring gotcha): a `git pull` moves
  `EXPECTED_GAM_VERSION` in `core/gam/commands.py` but **not** the gitignored
  vendored binary. If the pin and `command_catalog.json`'s version disagree (the
  session-start hook flags this), or the binary is missing, re-vendor:
  `make gam` then `python scripts/build_command_catalog.py`. See
  [docs/domains/build-packaging.md](../../../docs/domains/build-packaging.md).

## 3. Load the domain, not the whole map

CLAUDE.md is always loaded — the constitution. The area-specific knowledge is
**not**: read the [docs/domains](../../../docs/domains) runbook(s) for the area
you're about to touch (index: [docs/domains/README.md](../../../docs/domains/README.md)),
including its failure history, before you start. If a domain has prior entries in
[docs/failure-log.md](../../../docs/failure-log.md), read those too.

## 4. Live priorities

- [ROADMAP.md](../../../ROADMAP.md) — the ranked backlog and deliberate
  trade-offs.
- Open PRs / CI state if relevant to what you're picking up.
- If enough `fix:`/`revert:` commits or ledger entries have piled up, the
  `rules-improver-check.sh` hook (a separate SessionStart hook from the ORIENT
  one) nudges you to run [improve-rules](../improve-rules/SKILL.md); that's an
  observer pass, not this session's work — note it and move on.

## 5. Plan

Set out the task as a short todo list before the first edit. When it's a
mutation to a real tenant, the rule of engagement is explicit per-action
permission — plan the throwaway-account verification into the task.
