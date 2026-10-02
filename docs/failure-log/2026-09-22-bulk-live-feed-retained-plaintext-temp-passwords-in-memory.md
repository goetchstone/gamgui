# 2026-09-22 — Bulk live-feed retained plaintext temp passwords in memory

- **Symptom:** `OnboardJob.recent` (the polled ✓/✗ feed, bounded to 12) stored each hire's full
  result dict — including `credential.password` — so up to 12 plaintext temp passwords lingered in
  the retained job even after the sheet's one-shot/TTL clear. Never displayed, but held.
- **Cause:** `record()` did `self.recent.append(res)` with the whole result; the feed template only
  reads email/name/ok/errors.
- **Why not caught:** the sheet redaction was the focus; nobody asserted the *feed* copy was clean.
- **Fix:** append only `{email, name, ok, errors}` to `recent`; the password lives briefly only in
  `credentials`. Test `test_bulk_recent_feed_holds_no_plaintext_password`.
- **Prevention:** when a struct carries a secret, copy only the fields a consumer needs into any
  retained/rendered collection — never the whole struct.
