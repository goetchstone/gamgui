---
name: post-failure
description: Run after any regression, broken test, or bug caused by a change — especially a "the mock passed but the live tenant broke" break. Document it, reproduce it, fix it, and leave a tripwire so the same shape can't recur silently.
---

# Post-failure learning

Something broke. Before fixing it, document it — every failure is a lesson that
ratchets the codebase up a notch. This repo's defining failure class is
**"the mock passed, the live tenant broke"**; treat that as the default suspect.

## Step 1 — Root cause

1. **What broke?** The exact symptom (the operator, or a live tenant, saw).
2. **What caused it?** Which change, which line, which assumption.
3. **Why wasn't it caught?** A permissive mock, a missing test, an untested
   path, a flag the grammar rejects that the mock accepted.
4. **What would have prevented it?** A check, a test, a stricter mock, a rule.

## Step 2 — Reproduce before fixing

Produce a failing test (or, for a live-only break, a faithful mock case that
**fails the way real GAM fails** — check `gamgui/resources/gam7/GamCommands.txt`)
that reproduces the symptom before you write the fix. The reproduction is the
proof the fix targets the real cause, and it becomes the regression test. Keep
it, name it clearly, commit it. Remember: a greener mock is not proof a GAM
write works.

## Step 3 — Fix

Fix the actual issue. Verify: `.venv/bin/python -m pytest -q`.

## Step 4 — Leave a tripwire (pick the right home)

Match the lesson to its enforcement home (see
[FRAMEWORK.md](../../../docs/FRAMEWORK.md) §2):

- **A coding pattern to avoid** → add it to
  [pre-commit](../pre-commit/SKILL.md) (soft).
- **A permissive-mock or missing-command-syntax gap** → tighten
  `tests/fixtures/mock_gam.sh` to reject what GAM rejects, and/or tighten the
  generated grammar contract in `tests/test_command_contract.py` (tripwire — this
  is the same family as the drift guards).
- **A cross-cutting invariant issue** (an invariant was in the wrong layer, or
  none covered the shape) → append one entry to
  [RULE-FEEDBACK.md](../../../docs/RULE-FEEDBACK.md). Do **not** edit CLAUDE.md
  now — [improve-rules](../improve-rules/SKILL.md) reads the ledger later, with
  distance, and proposes the constitutional change as a PR.
- **A domain assumption** (how an area actually behaves) → update the relevant
  [docs/domains](../../../docs/domains) runbook.

## Step 5 — Log it

Add a five-field entry to [docs/failure-log.md](../../../docs/failure-log.md),
newest first: **symptom / cause / why not caught / fix / prevention**. Name the
tripwire in the prevention field — "nothing yet" is a valid and telling answer.
Touching the failure log is what clears the pre-commit hook's `fix:` block, so
do this before committing the fix.
