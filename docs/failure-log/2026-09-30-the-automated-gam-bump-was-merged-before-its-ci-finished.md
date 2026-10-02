# 2026-09-30 — The automated GAM bump was merged before its CI finished

- **Symptom:** a CI audit found PR #22 (GAM 7.48.11 → 7.48.14, opened by gam-watch) merged at
  17:46:37Z, 5 s after its CI and CodeQL runs were approved, and before any of their jobs finished (the
  last at 17:50:29Z). The post-merge push run on `main` was green, so nothing broke this time.
- **Cause:** nothing gated `main` — no branch protection, no ruleset — while gam-watch.yml's header
  said "the PR's own required checks … are the gate". A PR opened with `github.token` also starts no
  CI by itself: its runs sat as `action_required` until approved.
- **Why not caught:** the gate was a sentence in a comment; nothing checked that it existed.
- **Fix:** rulesets on `main` (no force-push or deletion; a PR gate requiring `ci-ok`, CodeQL's two
  Analyze legs and Dependency review, admin bypass for direct pushes), a `ci-ok` job that fails on any
  failed, cancelled or skipped blocking job, and gam-watch split so the new GAM runs with a read-only
  token. See build-packaging "Branch protection".
- **Prevention:** `test_ci_ok_needs_every_blocking_job`,
  `test_the_required_checks_always_report_on_a_pull_request`, `test_every_job_has_a_timeout`, and the
  coverage leg held to the matrix — each shown to fail on the break it guards. The rulesets
  themselves live in GitHub settings, not the repo: a test can hold the workflow side only.
