# 2026-09-23 — Bulk department Apply wrote an edited form under the previous preview's dialog

- **Symptom:** a review (F17) previewed Department "Sales" on alice, then pasted alice and carol,
  typed "Finance", and clicked the stale "Apply to 1 user". The dialog said "Set Department to
  'Sales' on 1 user(s)?"; both people got Finance.
- **Cause:** the Apply button posts `hx-include="#bulk-form"`, and `/users/bulk/apply` re-resolved
  the targets and the department from that live form; `confirmed=1` was its only check.
- **Why not caught:** the apply test posted the form it had previewed; nothing edited it in between.
- **Fix:** the preview holds its department and people under a single-use token
  (`web/previews.py`); Apply sets exactly those (titles re-read from the directory, as the job
  keeps each title), and refuses a used, expired or missing token, an edited department, list or
  group, or a previewed person no longer active ("preview again").
- **Prevention:** `test_bulk_store_apply_runs_only_what_was_previewed`,
  `test_bulk_store_apply_is_single_use`. Offline only; no GAM argv changed.
