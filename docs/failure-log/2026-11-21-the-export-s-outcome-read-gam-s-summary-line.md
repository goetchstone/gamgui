# 2026-11-21 — The Sheet export's outcome read GAM's one-line summary, and the mock lied twice more

- **Symptom:** found by an independent review of #174 before it merged; never seen live. Three ways the result screen
  (and Files) could say the wrong thing about whether a Sheet was made:
  1. A full Drive or a missing folder was matched as "Drive Folder: …, Failed", a line GAM never prints. GAM's
     `entityActionFailedWarning` runs after `Act.Set(Act.CREATE)`, and `Act.Failed()` is `"<action> Failed"`, so the
     line is "Drive Folder: root, Create Failed: <why>" (7.52.04, writeCSVToDrive; gamlib glaction). A full Drive
     read as "can't be told" instead of "Nothing was made".
  2. A read of an account GAM can't get a token for fails softly (`handleOAuthTokenError`: rc 50, `continue`), and the
     loop still reaches `csvPF.writeCSVfile`, so GAM uploads a Sheet of headers only, prints its link and exits 50. The
     mock's `not_a_user` printed no header, so its export uploaded nothing, and a test held that wrong answer.
  3. The outcome was read from `ChangeResult.detail`, which is `str(GAMError)`: one stderr line, of the most severe kind.
     A line the read printed about another account ("User: bob@…, Drive Service/App not enabled") could stand for the
     upload, and the screen said "Nothing was made" over a file that existed.
- **Cause:** the failure lines were written from the message table, not from the call that formats them; the mock's
  refused-token path was modelled for plain reads, where no one looked at stdout; and the outcome reused the error
  summary built for a different purpose.
- **Why not caught:** the outcome tests fed it the same hand-written lines the code matched, and the mock never printed
  any of the upload's failure lines.
- **Fix:** #174. `sheet_export.upload_state` reads the upload's own lines from stderr (naming the admin, the last one
  wins) into the record's `extra.upload`; `outcome` decides from that and the link, and uses the summary only to say why
  a made Sheet may be short. The mock prints the titles on a refused token (print delegates, print
  forwardingaddresses), and models a full Drive and a Sheets step refused after the create.
- **Prevention:** `tests/test_sheet_export.py`: the upload lines parametrized with GAM's real words,
  `test_the_failed_line_is_the_one_gam_prints`, `test_a_read_of_an_account_that_isn_t_there_uploads_its_headers`,
  `test_a_full_drive_makes_nothing_and_says_so`, `test_a_read_s_own_line_about_another_account_never_stands_for_the_upload`.
  Mock-proven only: the first live export is what proves the lines.
