# 2026-11-04 — A write could cut off the admin GamGUI signs in as

- **Symptom:** found by SwiftGamGui's review (its failure log, 2026-10-13, "Offboarding's own-admin check failed open";
  guard-audit runbook, "Built: the connected-admin check"), never seen live here. Reproduced on `main` through the routes:
  Users' Sign out, Suspend and Delete, the Builder's and a sequence's suspend, delete, reset and sign-out all ran on
  the admin's own address. Deprovision deletes GamGUI's OAuth token (failure log 2026-09-24), and suspend or delete ends
  the account it signs in as. Offboarding checked, but only by email, so an alias got past it, and an unreadable token
  skipped the check.
- **Cause:** the check lived in one flow (`core/lifecycle.py` `check_addresses`), compared addresses, and treated an
  unknown admin as "nothing to compare". Nothing held the writes themselves to it.
- **Why not caught:** the tests' token held no claims, so every admin was unknown and the check's fail-open branch was
  the only one run. No test tried these writes on the admin except offboarding's.
- **Fix:** branch `fix/connected-admin-at-the-chokepoint`.
  - `core/gam/commands.py` `cut_off_account(argv)`: delete, suspend, deprovision, a password change, a sign-out, by
    shape so a variant counts. Sign-out and the password change are in the set unproven, as in SwiftGamGui: refusing
    costs a trip to the Admin console, a wrong guess costs the app's authorization.
  - `GAMConnector.cut_off_refusal(commands)`, asked in `_run_write`, so every caller is covered and the refusal is
    audited. The admin's ID is the token's `sub`, kept only once `info user <admin> fields primaryEmail,id` agrees; a
    re-import asks again. Each address is read as it will be sent and compared by ID, so an alias or a rename can't
    pass. An unconfirmed admin refuses, and so does an address GAM can't read or that has no `id`.
  - The Users, Builder and offboarding previews ask too, so it is said before a confirm step is drawn; there an address
    no one holds passes, since an earlier step of a sequence may restore it, and the run reads it again. Unlike
    SwiftGamGui, which confirms at Check access, the admin is confirmed on the first such write.
- **Prevention:** `tests/test_connected_admin.py`. The tests' token now carries claims and the mock has
  `admin@example.com` with an `id`, so a cut-off write takes the real path. Every mock user has an `id`.
  - Not live-proven: `info user … fields primaryEmail,id` is confirmed live in SwiftGamGui only (its live-verification
    table), and whether `oauth2.txt`'s claims hold `sub` on every install is read from GAM's source, not seen here. An
    install without it refuses these writes until it re-imports, which is the safe direction.
  - Adversarially verified offline (541 buildable commands checked, 13 mutants all caught). It found a record read
    too loosely, argv variants the shape missed and a broken check that went unaudited; all fixed here. Left: the
    claims in `oauth2.txt` are trusted as stored, since whoever can forge that Keychain item already has the token.
  - The `gam-command-reviewer` pass found a denied Keychain read that 500'd a preview, the sequence above, and a refusal's
    reason collapsed under "GAM's error"; all fixed here.
  - The mock doesn't model GAM's address rewriting (`uid:<id>`, a bare name), so the run's read isn't tested on those.
