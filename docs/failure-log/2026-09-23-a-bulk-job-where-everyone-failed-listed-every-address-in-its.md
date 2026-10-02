# 2026-09-23 — A bulk job where everyone failed listed every address in its final panel

- **Symptom:** the 10/10 review (plan item B2) found `BatchJob.failed` unbounded: the bulk
  department job, the calendar-share subscribe fan-out, the Builder sequence and offboarding
  `.append`ed every failure, and `_bulk_apply.html` / `_calendar_subscribe_job.html` joined the whole
  list — a domain-wide run that failed for thousands of users rendered thousands of addresses (and
  the subscribe poll counted them with `| length`). Found by reading the code; not seen live.
- **Cause:** invariant #9 was applied to `BatchJob.log` and to signatures' `ApplyJob` /
  onboarding's `OnboardJob`, but the shared `BatchJob` had no failure cap of its own, so each route
  appended directly.
- **Why not caught:** the scale tests covered `ApplyJob` and the subscribe *log*; nothing drove a
  `BatchJob` past a cap with every item failing.
- **Fix:** `BatchJob.fail()` keeps the full `failed_total` and a sample capped at
  `FAILED_SAMPLE_CAP` (200, same as the other two jobs); the four routes call it, and the two
  final panels print the total plus "… +K more". This commit.
- **Prevention:** `tests/test_users_web.py` —
  `test_batch_job_failures_stay_bounded_and_render_the_overflow`,
  `test_run_subscribe_caps_its_failed_sample`, and the source tripwire
  `test_no_route_appends_to_a_batch_jobs_failed_list_directly`.
