# 2026-09-23 — The signature designer's "test" user was a colleague, not the operator

- **Symptom:** review F18, in a browser: on page load the scope was "Specific user (test)" and
  "Which" was `alice@example.com`, the first of the sorted directory. The page's default path,
  paste a template, Preview, "Apply to 1 user", overwrote that colleague's live signature (no
  backup). The preview did say "previewing as …", so it was visible, but the default did not
  behave like a test target.
- **Cause:** `sigPopulate()` filled "Which" from the sorted active-user list and the browser
  selected its first option. The app never knew who the connected admin was.
- **Why not caught:** `test_signatures_designer_defaults_to_one_user` checked the scope type
  (user, not company), not which user.
- **Fix:** `SecretsVault.oauth_admin_email` reads the admin's `email` claim from `oauth2.txt`
  (`decoded_id_token`, the key the vendored GAM build writes); the page preselects that address
  when it is an active user, and otherwise opens on a blank "Choose a user…" that matches nobody.
- **Prevention:** `test_users_web.py::test_signatures_test_user_is_the_connected_admin`,
  `test_signatures_test_user_is_an_explicit_choice_otherwise` (unknown, outside the directory,
  suspended), `test_vault.py::test_oauth_admin_email_reads_only_the_email_claim`; the select's
  behaviour was checked in a browser against the mock preview. Not proven live: the claim's
  location in a real `oauth2.txt` comes from the vendored build's key names. If it differs, the
  fallback is the blank choice, never a colleague.
