# 2026-09-23 — Onboarding's Run created an account nobody had previewed

- **Symptom:** a review (F15) previewed a tasks-only hire (the button read "Create the task list"),
  then ticked "Create the Google account", filled first/last, and clicked that same button. A real
  account was created — temp password, OU, signature, groups, calendars — none of it previewed.
  On `main` the same clicks were refused ("Preview first — creating an account needs confirmation").
  An email or assignee retyped after Preview, or the role template saved again, also ran unpreviewed.
- **Cause:** this branch's guard sweep (a31b146) replaced the account-specific gate (`confirm=1`
  rendered only when the preview showed account creation) with one `guard.enforce(confirm_step=True)`
  and made the `confirmed=1` hidden input unconditional, while the Run form still posted the live
  form (`hx-include="#onboard-form"`) and `/run` rebuilt the hire and re-read the role from the store.
- **Why not caught:** the run tests posted straight to `/onboard/run` with `confirmed=1`; none
  previewed one form and ran another, and the tripwire only checks that a bare POST writes nothing.
- **Fix:** `/preview` holds the hire, the role template and the welcome template under a single-use
  token (`web/previews.py`); `/run` runs exactly those and refuses a used, expired or missing token,
  or any field changed since the preview ("preview again").
- **Prevention:** `test_run_executes_only_the_previewed_hire` (account ticked, email/assignee
  retyped, welcome added → zero writes, nothing audited), `test_run_uses_the_role_as_previewed`,
  `test_run_is_single_use`. Offline only; no GAM argv changed.
