# 2026-09-24 — Bulk onboarding ran whatever came back, and a cancelled task-list build left no audit

- **Symptom:** (1) `/onboard/bulk/run` re-parsed the CSV text the page posted back and re-read the
  role templates at run time: a replayed POST re-sent welcome emails and re-created task lists, and a
  role edited after the preview changed what ran — the last confirm step without a held preview.
  (2) Quitting mid-build of a task list (`create_onboarding_runbook`, outside `_run_write`) left the
  list in the assignee's Google Tasks and nothing in the audit log: its handlers caught `Exception`,
  and cancellation is a `BaseException`.
- **Fix:** the bulk preview holds its valid rows and role templates under a single-use token and Run
  executes those (an edited CSV, a replay or an expired token is refused); `create_onboarding_runbook`
  records `ok=False`, `error=INTERRUPTED` with the tasks made so far before re-raising. The generic
  tripwire now applies an edit *after* the confirm step's own hidden fields, so an edit can't be
  masked by a hidden copy. Tests `test_bulk_run_executes_the_previewed_rows_and_roles`,
  `test_a_cancelled_runbook_build_is_audited_as_interrupted` (both fail on the old code).
- **Prevention:** every confirm step runs a held preview; every audited write path handles
  cancellation, not just `_run_write`.
