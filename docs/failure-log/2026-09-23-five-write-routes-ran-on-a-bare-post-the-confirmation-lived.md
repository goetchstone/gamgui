# 2026-09-23 — Five write routes ran on a bare POST: the confirmation lived only in the templates

- **Symptom:** the 10/10 review (plan item S4) posted straight to `/users/suspend/apply`,
  `/users/bulk/apply`, `/calendars/event/delete`, `/lifecycle/offboard/run` and `/signatures/apply`
  with no confirmation field, and each ran: a suspend, an event delete, a company-wide signature
  overwrite, a full offboarding. `/onboard/run` without `create_account` did the same (task list,
  groups, calendars, welcome email). Offline against the mock; not seen live. Reaching them needs
  the launch token and a same-origin request, so the exposure was a mis-wired control or a stale
  form, not a remote caller — but a guard the server doesn't check is not a guard.
- **Cause:** those routes called `guard.evaluate` (if at all) only to *render* the confirm step; the
  apply route trusted that the POST came from it. Builder, account delete and calendar delete had
  their own server-side checks, so the pattern existed — it just wasn't one helper every route used.
- **Why not caught:** every route test posted the happy path; nothing posted a bare POST and asserted
  zero writes, and nothing enumerated the routes, so a new write route inherited no check by default.
- **Fix:** `guard.enforce(previews, form, confirm_step=)` — destructive → `confirmed=1`, destructive
  and bulk → typed "confirm", `confirm_step` routes (bulk jobs, onboarding) → `confirmed=1` always —
  called before the first GAM write by suspend, bulk department, event delete, offboarding (as
  destructive to the leaver), signatures, onboarding single + bulk, and the Builder's run/sequence.
  The confirm steps post the field (`hx-vals` / hidden input; onboarding's `confirm=1` became
  `confirmed=1`). Every button that starts a write or a job now has `hx-disabled-elt` (plan U13), so
  a double-click can't start two offboardings. This commit.
- **Prevention:** `tests/test_write_routes_guarded.py` — enumerates every POST route from the app's
  OpenAPI schema (and cross-checks the `@router.post` count), requires each to be exempt-with-reason
  or GATED; a bare POST to each gated route must reach the mock with zero writes, the confirm step
  the preview actually renders must run the write, and every template control posting to a write
  must carry `hx-disabled-elt`. `tests/test_guard.py` covers `enforce` itself. Mock-proven only:
  whether each write works live is unchanged (README live-verification status).
