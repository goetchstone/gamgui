# 2026-09-23 — The offboarding page's first sentence left out the steps that lock the leaver out

- **Symptom:** the docs truth pass at the end of this branch found the Lifecycle page still
  introducing the routine as "reset password → delegate → auto-reply → transfer Drive & calendars →
  clear calendar shares → manager reminder" after "Revoke access & sign out" and "Turn off
  forwarding" became steps of their own (`4296aef`, `73f349a`). The form's boxes and the preview
  named eight steps; the sentence above them named six.
- **Cause:** the sentence was a hand-typed copy of the step list, while the page already received
  the real one (`lifecycle.STEP_NAMES`) for its "already done" boxes.
- **Why not caught:** `test_lifecycle_page_renders` counts the boxes, not the prose; both step
  commits updated the README and the runbook, which were checked, and not this template.
- **Fix:** the sentence joins `STEP_NAMES` in run order, so it names what the boxes and the preview
  name.
- **Prevention:** `test_lifecycle_page_intro_names_every_step_in_run_order`. Offline only; no argv
  changed.
