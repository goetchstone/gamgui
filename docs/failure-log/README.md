# Failure log

Institutional memory of what broke and why — so the same shape can't recur
silently. Read the recent entries before working in a domain that has prior
ones. Every incident that reaches a user (a broken test caught late, a live
tenant break, a regression) gets an entry via the
[post-failure skill](../../.claude/skills/post-failure/SKILL.md).

**One file per incident**: `YYYY-MM-DD-<slug>.md`, opening `# YYYY-MM-DD — <title>`.
A new entry is a new file, so two branches that each log an incident never
conflict (a single newest-first file made every pair of open PRs collide).
Newest first: `ls -r docs/failure-log/`. Cite an entry by its date and title,
as code comments already do ("failure-log 2026-09-23").

**Format**: five fields:

- **Symptom** — what was actually observed.
- **Cause** — the line / assumption that did it.
- **Why not caught** — the test/validation/gate gap.
- **Fix** — what shipped (link the commit/PR).
- **Prevention** — the tripwire test, hook, skill, or CLAUDE.md rule it fed
  (name it; "nothing yet" is a valid, and telling, answer).

This repo's defining failure class is **"the mock passed, the live tenant
broke"** — see [CLAUDE.md](../../CLAUDE.md) "The recurring failure mode". A fix
whose only proof is a greener mock is not proven; say so in the Prevention field.
