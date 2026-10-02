# 2026-09-24 — Deleting an account by an alias deleted the account that owns it

- **Symptom:** a re-verification typed an alias (`a.anders@example.com`, Alice's) into the Builder's
  "Delete account" and typed it back at the confirm step; the run reported done with argv
  `delete user a.anders@example.com`. The mock accepts it; real GAM resolves the alias and deletes
  Alice — an account the preview never named.
- **Cause:** the typed-email rule (`guard.enforce`) compares what was typed with the argv's address,
  which is only as good as that address; GAM's `delete user <UserItem>` accepts an alias
  (GamUpdate.txt: `no_action_if_alias` exists to stop exactly this).
- **Why not caught:** the mock treats every address alike; no test deleted by an alias.
- **Fix:** `guard.alias_deletes(directory, addresses)` names each address that is someone's alias; the
  Builder single and sequence previews refuse it (no Run, no token) and `/users/delete/apply` refuses
  it before any write. Fails closed when the directory can't be read. Tests
  `test_builder_refuses_to_delete_by_an_alias`, `test_user_page_delete_refuses_an_alias` (both fail on
  the old code). `noactionifalias` was not added: its exit code when it takes no action is unknown,
  and a 0 would turn a refusal into a misleading "deleted".
- **Prevention:** a destructive write addressed by a typed value must resolve that value to the
  entity it actually hits before the confirm step. Unproven live.
