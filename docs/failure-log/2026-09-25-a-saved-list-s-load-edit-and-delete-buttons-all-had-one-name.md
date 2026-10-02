# 2026-09-25 — a saved list's Load, Edit and Delete buttons all had one name

- **Symptom:** (re-check G3) on Signatures, every saved template's buttons were named "Load" and
  "Delete"; on Onboarding's roles, every role's "Edit" and "Delete". A screen reader's button list (or
  voice control's "click Delete") could not tell which template or role each acted on.
- **Cause:** `_sig_templates.html` and `_onboard_roles.html` rendered bare verbs; the item's name was
  only in the row's visible text beside them. The repeated-button rule (a row's Build/Copy, a delegate's
  Remove, a tray Stop carry an `sr-only` name) had been applied screen by screen, and these two lists
  were missed.
- **Why not caught:** axe has no rule for identical names on distinct buttons, and no default-run test
  read these partials' button names — the rule lived in the runbook, not in a check.
- **Fix:** each button carries its item, visually hidden (`Delete<span class="sr-only"> Sales</span>`),
  as `_command_row.html` and `_job_live.html` do. The header's `z-50` (review R2) was checked in
  headless Chrome over the Builder's `#row-actions` and the pickers' lists at the app's size and its
  minimum: it covers none of them, so the layering stayed.
- **Prevention:** `test_a_saved_lists_repeated_buttons_name_their_item` renders both lists with two items
  and fails on a shared button name; `test_the_header_draws_over_no_popup_opened_in_main` holds the
  header off `<main>`'s popups with `elementFromPoint`, and proves its probe sees a menu moved onto the bar.
