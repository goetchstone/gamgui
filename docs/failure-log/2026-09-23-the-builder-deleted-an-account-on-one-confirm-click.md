# 2026-09-23 — The Builder deleted an account on one Confirm click

- **Symptom:** a review (F2) found `/builder/run` with `build.delete_user` — the "Delete account"
  row action on every Builder result table — needed only `confirmed=1`, while `/users/delete/apply`
  demanded the exact email typed. A sequence step deleting an account was the same. The Builder path
  also skipped the Users zone's warning about an unfinished Drive/Calendar transfer, which is the
  state an offboarded account is in for 10–25 minutes.
- **Cause:** the typed-email rule lived in one route (`users.py` compared `confirm` to the email),
  not in `guard.enforce`, which only knew risk and count; the guard docstring described account
  delete as its own stronger gate, which was true for one of its three paths.
- **Why not caught:** the tripwire gated `/builder/run` with a suspend; the Builder delete test
  asserted that `confirmed=1` alone ran the delete.
- **Fix:** `guard.enforce` requires each address an account-delete preview deletes (its argv is
  `GAMCommands.delete_user`) among the posted `confirm_email` values, on top of the click. The
  Builder and sequence previews render that input and the pending-transfer warning;
  `/users/delete/apply` now calls `enforce` too (its form posts `confirm_email` and `confirmed`).
- **Prevention:** `test_enforce_an_account_delete_needs_its_address_typed`,
  `test_enforce_each_deleted_account_is_typed`, `test_builder_delete_needs_the_email_typed`,
  `test_a_sequence_that_deletes_an_account_needs_the_email_typed`,
  `test_builder_delete_warns_on_a_pending_data_transfer`. Offline only; `delete user` is unchanged.
