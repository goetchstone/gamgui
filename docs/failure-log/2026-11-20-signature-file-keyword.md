# 2026-11-20 — A signature that is one of GAM's file keywords

- **Symptom:** none seen live. Found while building Put Back (roadmap 4.1), by comparing with SwiftGamGui, which already
  refuses it. A signature body of `file`, `htmlfile`, `textfile`, `gdoc`, `ghtml`, `gcsdoc` or `gcshtml` (in any case,
  with spaces or `_`) went to GAM as `user X signature <body> html`.
- **Cause:** GAM's getStringOrFile reads such a value (stripped, lowercased, `_` removed: SORF_FILE_ARGUMENTS, read from
  the vendored build) as "load the next argument as a file or Google Doc". The next argument is `html`: a missing file
  ends the write with FILE_ERROR_RC (6), and a file named `html` in GAM's working folder would have become the person's
  signature. `GAMCommands.set_signature` passed any body through, and no screen checked.
- **Why not caught:** the mock took the value as the signature, as it took any string: more permissive than GAM.
- **Fix:** `GAMCommands.set_signature` refuses such a body (`signature_read_as_file`), so no path can send one. The
  Signatures preview names who the template renders it for and holds nothing; the Users page and the Builder say so in
  words; onboarding's signature step fails as a step, with no GAM call.
- **Prevention:** the mock now fails as GAM does (exit 6, the next argument read as a file). Tests:
  `tests/test_signature_file_keyword.py`. The builder is the tripwire: every signature write goes through it.
