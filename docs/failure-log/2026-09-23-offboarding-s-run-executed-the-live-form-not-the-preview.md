# 2026-09-23 — Offboarding's Run executed the live form, not the preview

- **Symptom:** reading offboarding before its first live run (plan U2) found the preview's Run button
  posted `hx-include="#offboard-form"` and the run route rebuilt the steps from it. Change the manager
  (or the subject, or the days) after Preview, click Run on the preview still on screen, and the
  routine ran against values nobody had previewed. Separately, an emptied subject showed the default
  in the auto-reply block but the vacation command sent `subject ''`. Not seen live.
- **Cause:** the preview and the run each built the steps independently from whatever the form held
  at that moment; nothing tied a run to the preview it came from.
- **Why not caught:** every test posted the same values to both routes, so the two builds always
  agreed; nothing edited the form in between.
- **Fix:** the preview holds the steps it built under a single-use, 15-minute token posted by the Run
  button; Run refuses a missing/used/expired token and a live form that differs from the previewed
  one, re-checks the addresses, and runs the held steps. The subject/message defaults are applied
  before the steps are built. This commit.
- **Prevention:** `test_offboard_run_executes_exactly_the_previewed_commands` (the run's writes, as
  `gam …` lines, equal the preview's), `test_offboard_run_refuses_a_form_edited_after_the_preview`,
  `test_offboard_run_needs_a_fresh_unused_preview`, `test_offboard_run_rechecks_the_directory`,
  `test_offboard_preview_runs_the_default_text_for_an_emptied_field`.
