# 2026-11-18 — ci-ok failed a run whose every job had passed, after "re-run failed jobs"

- **Symptom:** on PR #137, a macOS job no runner picked up was re-run with "re-run failed jobs"; it passed, and
  ci-ok still failed: "no job in the list for gam-compat", "… for app", "… for fuzz". Every job of the run had
  passed. Each further partial re-run failed the same way, so on a day the account's macOS runners were scarce
  (two other repositories' CI), a PR could pass only by getting all seven macOS jobs through in one attempt.
- **Cause:** `ci-ok` listed the jobs of its own attempt (`runs/<id>/attempts/<n>/jobs`). A partial re-run copies
  the jobs it doesn't re-run into the new attempt's list, but only later: when ci-ok asked, attempt 4's list held 8
  of its 13 jobs, with `total_count` 8, so the gate's truncation check couldn't see the gap. Minutes later the same
  list held all 13.
- **Why not caught:** `tests/test_ci_gate.py`'s `rerun_passed` fixture was a full re-run, whose attempt lists every
  job; no fixture was a re-run of the failed jobs, and the workflow test held the per-attempt URL as correct.
- **Fix:** ci-ok lists every attempt's jobs (`jobs?filter=all`) and `ci_gate.newest` judges each job by its newest
  attempt: a job re-run and dropped now still fails, one not re-run keeps the result it had. The earlier attempts'
  lists are complete, so a carried-over job is never missing.
- **Prevention:** the `partial_rerun_listed_late` fixture (GitHub's real answer for run 44318933480, attempt 4 as
  ci-ok saw it) and its four tests in `tests/test_ci_gate.py`; `test_ci_ok_needs_every_blocking_job` now holds the
  `filter=all` URL and refuses a per-attempt one.
