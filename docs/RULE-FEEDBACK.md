# Rule feedback ledger

Low-friction notes on how [CLAUDE.md](../CLAUDE.md)'s invariants performed in
practice. Appending costs one entry and commits to nothing — that low friction
is what keeps signal flowing. **Sessions append here; they do not edit
CLAUDE.md.** The [improve-rules](../.claude/skills/improve-rules/SKILL.md)
observer pass reads this pile later, *with distance*, and proposes at most one
focused constitutional edit as a PR. See [FRAMEWORK.md](FRAMEWORK.md) §3.

When to add an entry: an invariant almost let something through, was in the
wrong enforcement layer (a rule said it but nothing enforced it where it broke),
was worded loosely enough to permit a bad reading, or a failure hit a shape no
invariant covers at all.

**Format** — newest first:

```
## <date> — <one-line title>
- **What happened:** <the incident / near-miss; link commit/PR/failure-log entry>
- **Invariant in force:** <#N and its name, or "none — new shape">
- **Why it didn't hold:** <wrong layer / weak wording / not covered / not a rules problem>
- **Would a rule have caught it?** <no / only if enforced differently / yes but unworded>
  — the "no" and "only if enforced differently" answers are the valuable ones.
- **Enforcement home if changed:** <skill (soft) / hook (hard) / tripwire test (backstop)>
```

The most valuable entries are the ones that answer *"only if enforced
differently"* — the invariant already exists and is simply living in the wrong
layer. Moving it (skill → hook → tripwire) is a no-text-change fix.

---

## 2026-09-15 — Secret redaction covers `argv` but not the audit record's `extra.error`
- **What happened:** An adversarial review of the onboarding `notify` change noted that `_run_write`'s
  failure path records `extra={"error": str(exc), …}` (`gamgui/core/connectors/gam_connector.py`), and
  that string is **not** run through `redact_argv` the way `argv` is. It's safe today — `GAMError`'s
  message excludes the submitted argv and GAM doesn't echo a submitted password in its stderr — so no
  secret reaches `audit.jsonl` now.
- **Invariant in force:** #4 (secrets never persisted outside the Keychain) / #2 (the audited chokepoint).
- **Why it didn't hold:** wording/coverage. "Secrets are redacted before audit" is enforced only on the
  `argv` field; a future GAM version (or a new command) that echoed a submitted secret value in stderr
  would land it unredacted in `extra.error`.
- **Would a rule have caught it?** Only if enforced differently — a redactor applied to *every* audited
  field (or a tripwire asserting no audited field carries an un-redacted value) rather than to `argv`
  alone.
- **Enforcement home if changed:** tripwire test + a one-line scrub of `extra.error` through the
  redactor in `_run_write`. Low priority (no live exposure today) — for the observer pass.

## 2026-09-15 — Invariant #2 reads absolute, but audited mutations exist outside `_run_write`
- **What happened:** While writing the domain runbooks, the adversarial verifiers found that
  `create_onboarding_runbook` (`gamgui/core/connectors/gam_connector.py`) runs its `create tasklist` /
  `create task` GAM calls via `runner.run_authenticated(..., serialize=True)` and calls
  `self.audit.record` **directly** — skipping `ChangePreview` and `guard.evaluate()`, and auditing only
  a summary (not each task). `reset_password`'s best-effort follow-up `signout_user` is serialized but
  **not audited at all** (fire-and-forget).
- **Invariant in force:** #2 ("Every mutation goes through the chokepoint … There is no second write
  path. Don't add one.").
- **Why it didn't hold:** wording. These are deliberate, low-risk, serialized shapes (a two-step call
  that needs the returned tasklist id; a fire-and-forget sign-out) — but #2 reads as absolute, so a
  reader can't tell an accepted exception from a violation, and the audit log isn't the complete record
  #2 implies.
- **Would a rule have caught it?** Only if enforced differently — a tripwire test asserting every
  mutation/`audit.record` call routes through `_run_write` except an explicit allowlist would make the
  exceptions visible and catch a genuinely rogue new one.
- **Enforcement home if changed:** tripwire test (allowlist of non-`_run_write` audited mutations) plus
  a one-clause sharpening of #2's wording to name the audited-exception shape. For the observer pass —
  not this session.

<!-- Add new entries ABOVE this line, newest first. -->
