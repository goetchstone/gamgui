# 2026-09-23 — Two offboardings of the same leaver could run at once

- **Symptom:** a review (F13) held two previews for carol → alice and posted both Run tokens: both
  jobs started and interleaved against the mock — two resets, sign-outs, delegates, auto-replies,
  transfers, domain-wide sweeps and reminders. Separately, a reload during the up-to-60-minute sweep
  lost the only progress view, and a fresh preview didn't say a run was in progress (it did say the
  manager was already a delegate, and following that queued a second run).
- **Cause:** nothing recorded which leaver a running job was for; the job id lived only in the
  page's DOM.
- **Why not caught:** each offboarding test ran one job; the single-use token stops a double-click
  on one preview, not two previews.
- **Fix:** `AppState.offboard_jobs` maps a leaver to their running job. While it runs, the preview
  and Run for that leaver are refused, showing the running job's own progress panel — which is also
  the way back to it after a reload. The Run's check and registration have no `await` between them.
- **Prevention:** `test_offboard_refuses_a_second_run_for_a_leaver_whose_offboarding_is_running`
  (holds the first run on its first step, previews and runs again, then lets it finish).
