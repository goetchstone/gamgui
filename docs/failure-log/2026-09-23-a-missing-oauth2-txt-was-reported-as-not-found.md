# 2026-09-23 — A missing oauth2.txt was reported as "not found"

- **Symptom:** `classify_stderr("ERROR: oauth2.txt file not found")` — and GAM's own "Client OAuth2
  File: …/oauth2.txt, Does not exist" — returned `NOT_FOUND`: the operator would read "The requested
  user, group, or resource was not found" instead of "Complete the setup wizard".
- **Cause:** `_PATTERNS` is first-match-wins and the generic not-found pattern sat above the
  `oauth2\.txt.*not found` one, which could therefore never match.
- **Why not caught:** no test classified a credentials-file line.
- **Fix:** the credentials-file rule (now also GAM's "OAuth2 File: … Does not exist" shape) moved
  above the not-found pattern.
- **Prevention:** `test_a_missing_credentials_file_is_not_authenticated_not_a_missing_user`. GAM's
  wording is read from the vendored build's bytecode, not captured live.
