# 2026-09-23 — Backup codes, browser tokens and file downloads ran from the Builder with no audit trail

- **Symptom:** the 10/10 review (plan item S9) confirmed that auto-promotion made `show`/`print
  backupcodes` (2-Step Verification bypass codes for any user), `show`/`print browsertokens` (Chrome
  enrollment tokens) and `get drivefile`/`get document` (any user's file contents) one click away in
  the Builder, and that running them left nothing in the audit log — a read of account-takeover
  material was indistinguishable from never having happened. Found by reading the code; not seen live.
- **Cause:** the "reads are open, only writes are audited" trade-off assumed a read's worst case is
  disclosure *to the operator*. These reads' output is itself a credential or a document, so who
  pulled them, and when, is exactly what an audit is for.
- **Why not caught:** the auto-promotion boundary test checks only that a promoted command is
  `READ_ONLY`; nothing classified reads by what their output is.
- **Fix:** operator decision D3 — keep them buildable, audit them. `SENSITIVE_READS` in `catalog.py`
  flags the six by verb + object (`CatalogCommand.sensitive`); `GAMConnector.catalog_read` records
  `sensitive_read` (target, argv, `extra.command`, ok/error — never the output) and a `todrive`
  export of one is `sensitive_export`. This commit.
- **Prevention:** `tests/test_builder.py` — `test_sensitive_reads_are_flagged_and_still_buildable`
  pins the six syntax heads (a GAM bump that renames one fails instead of silently losing its
  audit), `test_a_sensitive_read_is_audited_without_its_output` greps the audit file for the mock's
  canned codes, plus the failed-read and export cases; `AUDITED_READS` in the `audit.record` tripwire.
  Still unaudited: a sensitive read inside a Builder sequence is recorded only as `apply` (argv
  intact), and the CSV download of a tabular result.
