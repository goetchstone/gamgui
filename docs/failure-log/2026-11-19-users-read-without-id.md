# 2026-11-19 — The directory read never asked for `id`, and the mock gave it anyway

- **Symptom:** caught in review before merge. The Inactive → suspend preview (roadmap 3.3) leaves out the admin GamGUI signs
  in as, matching by address or by Google account ID. The ID half could never match on a real tenant, so a renamed admin
  would be listed as a target and counted in the number the operator types. The chokepoint still refuses that write, so
  the run would have ended with one unexplained failure. Classroom's owner column (`_owners`, labelling owner IDs from the
  cached directory) also never had an ID to match live, and showed no owner names.
- **Cause:** `CACHE_FIELDS` didn't name `id`. The Directory API returns only the fields asked for, and GAM adds no `id`, so
  `GAMUser.raw` had none.
- **Why not caught:** the mock's `print users` returned every fixture record whole, whatever `fields` named, so tests
  matched on an `id` real GAM would never send. The suspend's unit tests also built `GAMUser(raw={"id": …})` by hand.
- **Fix:** `id` is in `CACHE_FIELDS`. The mock's `print users` drops `id` from each record unless `fields` names it.
- **Prevention:** the stricter mock is the tripwire: a read that matches on `id` without asking for it now fails offline
  (`test_a_renamed_admin_is_left_out_by_its_account_id`, which also checks the argv names `id`).
  `test_the_mock_prints_users_as_gam_writes_them` now expects the list read's records without `id`. Only `id` is filtered:
  other unrequested fields still come back, and modelling GAM's whole field map in the mock is not done.
