---
name: improve-rules
description: The observer pass over accumulated evidence that proposes ONE focused edit to CLAUDE.md as a PR. Run when the session-start nudge trips, or on demand when the invariants feel stale. Never edits CLAUDE.md directly on main.
---

# Improve the rules

This is the **outer** skill. The inner skill is [CLAUDE.md](../../../CLAUDE.md)
itself — the constitution every session reads. This one observes how that
constitution performed and proposes a change to it. Two properties make it work;
drop either and it becomes rule bloat.

**It runs with distance.** Not mid-task, not while the incident is warm. The
agent that just got burned is the worst judge of whether its bruise deserves a
constitutional invariant. Evidence accumulates in
[RULE-FEEDBACK.md](../../../docs/RULE-FEEDBACK.md) and
[failure-log.md](../../../docs/failure-log.md); judgment happens here, later,
over the pile.

**It proposes, it does not decide.** Output is a PR against `main`, one focused
edit, for a human to accept or reject. Nothing here writes to `main`.

## What counts as feedback

This is a solo, mostly-single-operator repo — there is no PR-review signal to
mine. The substitute is **the commit that had to clean up after the last one**:
a `fix:` or `revert:` commit is literally "what the agent proposed versus what
reality required." Read all four sources since the last run:

1. **Fix/revert commits.**
   ```bash
   git log --oneline --since="$(cat .claude/.rules-last-run 2>/dev/null || echo '6 weeks ago')" \
     --format='%h %s' | grep -iE '^[a-f0-9]+ (fix|revert)'
   ```
   For each, read the diff. The question is never "was this a bug" — it's
   **"which invariant was in force, and why didn't it hold?"**
2. **The ledger** — `docs/RULE-FEEDBACK.md`, entries since the last run.
   Entries answering *"only if enforced differently"* are the highest value:
   the invariant exists and is simply in the wrong layer.
3. **The failure log** — `docs/failure-log.md`. A recurring symptom shape across
   entries is a candidate for a new invariant or a new tripwire.
4. **Tripwire / hook / CI firings.** A drift guard (`test_required_command_tokens_present`,
   `test_catalog_matches_grammar`, `test_pinned_version_consistent`), a
   mock-lies test, or a `.claude/hooks/*` block that fired and caught something
   is evidence its rule earns its place. `gh run list --status failure` if CI is
   in play.

## The judgment

Sort each piece of evidence into exactly one. Most land in the middle two — that
is the useful finding.

| Finding | Meaning | Action |
|---|---|---|
| **New failure mode** | No invariant covers this shape | Propose a new numbered invariant (with its origin) |
| **Known mode, wrong layer** | An invariant says it; nothing enforces it where it broke | Move enforcement: skill → hook → tripwire test ([FRAMEWORK.md](../../../docs/FRAMEWORK.md) §2). **No text change.** |
| **Known mode, weak wording** | The invariant is right but permitted this reading | Sharpen its text; keep it checkable |
| **Not a rules problem** | The rules were fine; something else failed | Record in the ledger, change nothing |

**"Not a rules problem" is a real and frequent verdict.** A pass that proposes a
change every time is accreting, not learning. "No change warranted, here's why"
is a successful run.

## The bar for any proposed edit

- **Cite the incident** — commit SHA, PR, or ledger/failure-log entry. No
  citation, no rule: invariants earn their place by surviving incidents.
- **Name the enforcement home** — a skill (soft), a `.claude/hooks` hook (hard),
  or a tripwire test (backstop, the drift-guard/mock-lies family). An invariant
  with nowhere to live is a wish.
- **Prefer strengthening an existing invariant over adding a number.**
- **Preserve the numbering.** CLAUDE.md's invariants are cited by number here and
  in the domain runbooks. Don't renumber or reuse a number. Retiring one: demote
  it into its [docs/domains](../../../docs/domains) runbook with the number's
  meaning intact, and record it under a `## Retired` note in CLAUDE.md with the
  evidence, so the next pass doesn't re-litigate it.
- **One change per PR** — judged and revertible on its own.

## Retirement

Same bar, reversed. An invariant stops earning its slot when its code path is
gone, its guard has never fired and the class hasn't recurred, or nothing cites
it. Check citations (`git grep -n "invariant <N>\|#<N>"`) before proposing
removal.

## Running it

```bash
git checkout -b chore/improve-rules-<yyyymmdd>   # never edit main
```

Make the single edit. Then **demonstrate it**: re-run the check that would have
caught the original incident and confirm it fails without the change and passes
with it — a rule proposed without that demonstration is a guess. PR body: the
evidence, the finding category, what it would have prevented. Stamp the window
so the next pass reads forward:

```bash
date -u +%Y-%m-%d > .claude/.rules-last-run
```

Then stop. A human merges it; the next session inherits it.
