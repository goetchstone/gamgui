# 2026-11-05 — A line break in a value split its CSV row

- **Symptom:** found in review of the connected apps read (back-port plan B1), never seen live. An app named `Mail
  Helper<U+2028>Tools` with all of Gmail read as an app with no scopes, plus a bogus app named "False" granted by "Tools",
  and the summary said no app has broad access. Reproduced in `tests/test_runner.py` for U+2028, U+2029, U+0085, VT, FF, FS,
  GS and RS. Any `formatjson` read is the same shape: the row's JSON cell is cut, so the row is dropped. So a user whose
  name holds one goes missing from the directory.
- **Cause:** `strip_cfgdir_noise` split stdout with `str.splitlines()` and rejoined it with `\n`, turning each of those
  characters into a row break. GAM's `QUOTE_MINIMAL` writer doesn't quote them, and `parser.py` had already been fixed for this
  (found by fuzzing). It runs on every real call, since GAM finds no `gam.cfg` in the per-call `GAMCFGDIR` and prints its banner.
  `_csv_from` had the same split, on a report with only addresses and numbers.
- **Why not caught:** the mock printed no banner, so no test's output ever reached the changed path. The fuzzing that found
  `parser.py`'s case calls the parser directly, after this step.
- **Fix:** branch `fix/cfgdir-noise-line-breaks`. All of them split on `\n` only. A banner-stripped output now keeps its last
  newline, as one with no banner already did.
- **The readers of free text had the same split.** `Vacation.from_show_text` let a subject holding one of these set a field, so
  `x<U+2028>  Enabled: False` read an attacker's auto-reply as off, the harm of 2026-11-04's entry. `_parse_signature` read a
  body as ending there, so saving it back lost the rest. SwiftGamGui's `signatures.md` records that signature reading as
  GamGUI's, so its fixture refresh should drop it.
- **Also found by the stricter mock:** `GAMRunner.version` never stripped the banner. Its folder is persistent, so GAM prints
  the banner only when it first makes the config there, and that first read's "version" was the banner's first line: a
  false version warning on Setup, and the banner on Home. It strips now.
- **Prevention:** `test_strip_cfgdir_noise_breaks_lines_only_where_gam_did`, `test_a_line_break_in_a_subject_sets_no_setting`,
  `test_a_signature_with_a_line_break_reads_whole`. The mock prints GAM's banner on every call with a
  `GAMCFGDIR`, so every test's read now takes this path. Offline only: whether Google lets an app or person's name hold these
  characters isn't known, and the fix doesn't depend on it.
