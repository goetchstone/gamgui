# Failure log

Institutional memory of what broke and why — so the same shape can't recur
silently. Read the recent entries before working in a domain that has prior
ones. Every incident that reaches a user (a broken test caught late, a live
tenant break, a regression) gets an entry via the
[post-failure skill](../.claude/skills/post-failure/SKILL.md).

**Format** — newest first, five fields:

- **Symptom** — what was actually observed.
- **Cause** — the line / assumption that did it.
- **Why not caught** — the test/validation/gate gap.
- **Fix** — what shipped (link the commit/PR).
- **Prevention** — the tripwire test, hook, skill, or CLAUDE.md rule it fed
  (name it; "nothing yet" is a valid, and telling, answer).

This repo's defining failure class is **"the mock passed, the live tenant
broke"** — see [CLAUDE.md](../CLAUDE.md) "The recurring failure mode". A fix
whose only proof is a greener mock is not proven; say so in the Prevention field.

---

## 2026 — GAM print/show commands reject `formatjson`

- **Symptom:** several `print`/`show` commands (e.g. `print messages`,
  `print delegates`, `show vacation`, `show signature`) failed against a live
  tenant with a usage error, though the mock accepted them.
- **Cause:** those subcommands don't support the `formatjson` output flag we
  append by default for machine-readable parsing.
- **Why not caught:** `tests/fixtures/mock_gam.sh` accepted `formatjson` on any
  command — the mock was more permissive than real GAM, which converts a live
  break into a green test.
- **Fix:** stopped sending `formatjson` on the commands that reject it; parse
  their text output instead.
- **Prevention:** check every new command's flags against the vendored grammar
  `gamgui/resources/gam7/GamCommands.txt` (the source of truth, not memory), and
  make the mock reject what real GAM rejects. Captured as the standing
  invariant "the mock lies" in CLAUDE.md and the memory `gamgui-gam-formatjson`.

<!--
Add new entries ABOVE this comment, newest first. Keep the five-field shape.
One entry per distinct incident.
-->
