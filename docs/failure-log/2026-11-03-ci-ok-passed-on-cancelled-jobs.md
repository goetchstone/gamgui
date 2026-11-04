# 2026-11-03 — ci-ok passed with four of its jobs cancelled

**Corrected 2026-11-04: the cause, the fix and the check below are wrong, and kept as the record.** The next
run with jobs no runner picked up passed `ci-ok` again, with this fix in place. See the entry of
2026-11-04, "ci-ok still passed on jobs no runner picked up".

- **Symptom:** on the Dependabot PR #70 (run 41491513163, 2026-11-02) four blocking jobs, the `.app`
  build, pinned-binary gam-compat, macOS 3.12 and a11y, were never picked up by a runner ("The job was
  not acquired by Runner of type hosted even after multiple attempts", a macOS arm64 capacity notice)
  and ended `cancelled` after 15 minutes. `ci-ok`, the required check, passed anyway, and the PR showed
  as mergeable. Auto-merge would have landed it with the bundle never built.
- **Cause:** `ci-ok` runs `if: always()`, but its one step's `if:` (`contains(needs.*.result,
  'failure') || … 'cancelled' || … 'skipped'`) had no status function, so Actions ANDs it with
  `success()`. That was false once needed jobs were cancelled, so the step was skipped and the job
  passed. With a *failed* need the step does run: all six earlier red runs checked ended with ci-ok
  failing.
- **Why not caught:** `test_ci_ok_needs_every_blocking_job` checked that the condition names
  `cancelled`, not that the step can run when a need is cancelled. A text scan can't evaluate Actions'
  expression rules, and every red run until this one had failed, not been cancelled.
- **Fix:** branch `fix/ci-ok-cancelled-needs`. The step's `if:` is `always() && (…)`.
- **Prevention:** `test_ci_ok_needs_every_blocking_job` requires the step's `if:` to start with
  `always() &&` (it fails on the old workflow). The build-packaging runbook says why. Checked on
  GitHub, where the text scan can't reach: the fix PR's first CI run (41652222524) was cancelled
  mid-flight on purpose, ten blocking jobs ended `cancelled`, and `ci-ok` ran its step and failed.
