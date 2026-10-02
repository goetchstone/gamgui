# 2026-09-24 — Bulk onboarding refused every CRLF CSV (a regression from its held preview)

- **Symptom:** a final check replayed the bulk page the way a browser does: a CSV with CRLF line
  endings — what Excel, Google Sheets and Python's `csv` write — always got "The CSV changed after the
  preview"; a CR-only file got no Run at all. Introduced hours earlier by `c667dc9`.
- **Cause:** the held preview was keyed on the raw uploaded text, and Run compared it with the CSV
  posted back from a hidden `<textarea>`, whose value a browser rewrites (CRLF/CR → LF, a leading
  newline dropped).
- **Why not caught:** TestClient posts the text back byte-for-byte; no test round-tripped it through
  a textarea's value.
- **Fix:** `_csv_key` normalises line endings (and blank edge lines) on both sides — at upload, before
  parsing and holding, and on the posted text. Test
  `test_bulk_run_accepts_a_crlf_csv_after_the_textarea_round_trip` (fails on `c667dc9`). Same round:
  the alias check now resolves each typed address with GAM's own `info user`
  (`GAMConnector.primary_address`) instead of the 5-minute cache, which missed secondary-domain
  aliases (the mock now resolves aliases like the Directory API); the offboarding panel reads
  "interrupted" from an explicit `job.interrupted` flag, not from the word in the last log line; and
  the Keychain secret cache moved to `clock.now()`.
- **Prevention:** a value a page posts back from a form control is what the *browser* sends, not what
  the server rendered — compare normalised values, or hold state server-side and compare nothing.
