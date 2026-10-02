# 2026-09-23 — Keep attachments, ChromeOS device files and photos downloaded with no audit record

- **Symptom:** review F4/F27, proved through `/builder/run`: `get drivefile` wrote a
  `sensitive_read` record, but `user alice@example.com get noteattachments notes/abc123` (a user's
  Keep attachments) and `get devicefile` ran with none. Both are auto-promoted, buildable reads
  that save someone's content to disk, exactly like the Drive download the audit was built for;
  `get photo`/`profilephoto`/`contactphotos` too. SECURITY.md and the ROADMAP said "file
  downloads" are audited.
- **Cause:** `SENSITIVE_READS` was a hand list of verb + object pairs, and it named only the two
  downloads operator decision D3 mentioned (`get drivefile`/`document`). Nothing tied the list to
  the grammar, so the other seven `get` reads fell outside it.
- **Why not caught:** the pin test (`test_sensitive_reads_are_flagged_and_still_buildable`) checked
  the list resolved to itself; nothing asked the grammar which reads download.
- **Fix:** downloads are flagged by rule: every read whose verb is `DOWNLOAD_VERB` (`get`, GAM's
  download verb). Only the two secrets (backup codes, browser tokens) stay a named list
  (`SECRET_READS`), since nothing in the grammar marks a secret. SECURITY.md now lists exactly
  what is audited, and what is not (other content reads, by design).
- **Prevention:** `test_command_contract.py::test_the_download_verb_is_what_saves_a_local_file`
  (every `get` stanza takes `targetfolder`; no other buildable read can emit a local-file option;
  every `get` read is sensitive), `test_builder.py::test_every_download_read_is_sensitive_by_rule`,
  `test_a_note_attachment_download_is_audited`, and the pin now names all thirteen heads, so a
  GAM bump that adds a `get` read fails until SECURITY.md lists it. Offline only: the audit record
  is ours; whether each `get` really saves a file is read from the grammar, not observed live.
