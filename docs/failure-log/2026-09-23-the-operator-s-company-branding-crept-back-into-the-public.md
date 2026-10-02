# 2026-09-23 — The operator's company branding crept back into the public repo

- **Symptom:** a review (plan U3) found the operator's company in the public UI and seed data after
  the June de-branding: its town as the Department placeholder, its email domain in the bulk-edit
  placeholder, its vendor names in the seeded onboarding role and the steps placeholder, an internal
  calendar name in the Calendars placeholder and a connector comment, a real employee's address in
  a test, and a local home path in `abapit_connector.py`'s docstring. The UI also still spoke of a
  "store" (one tenant's department model), not a department.
- **Cause:** new screens were written with the operator's real data as examples; the de-branding rule
  lived only in session memory, not in anything that runs on a commit.
- **Why not caught:** no check looked at added lines for the operator's terms.
- **Fix:** generic `example.com` / "Sales" / "Training Calendar" placeholders, fixtures and tests; a
  neutral seed role (CRM login, laptop, team channel); the UI says "Department" ("Bulk: set
  department", "Save title & department"); the local path is gone from the docstring. Git history
  keeps the old strings (rewriting it was not proposed).
- **Prevention:** the local pre-commit hook's private-term tripwire (`.claude/private-terms`,
  gitignored, so the terms never enter the repo) refuses a commit that adds one; the
  web-screens-jobs runbook's traps list says placeholders stay generic.
