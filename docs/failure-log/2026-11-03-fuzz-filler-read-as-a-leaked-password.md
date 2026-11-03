# 2026-11-03 — A filler token read as a leaked password (fuzz false positive)

- **Symptom:** CI's `fuzz (atheris)` job failed on the GAM 7.50.00 bump (PR #71, run 41625825527):
  `test_an_echoed_password_never_reaches_the_operator` asserted `secret not in shown`. Nothing leaked.
  The echo read `Command: gam PASSWORD ***redacted*** >>>…<<< …`: the password was nine of one character,
  and the next token, a random filler, was ten of the same character.
- **Cause:** the property skips a case where "the surrounding text happens to say it", but it looked only
  at the message and repr of an error read from the scrubbed stderr. Those never hold the echo's other
  tokens, and `_FILLER`'s free text (up to ten characters, any but "password") can hold a value drawn from
  `_SECRET_CHAR`. The gap dates from the property's start (#24, 2026-09-30): the same input fails at
  9c1bb72, before #69 touched this file.
- **Why not caught:** the suite's derandomized profile never draws a filler that holds the value. The
  fuzz job's seed is fixed, but its path follows coverage of `gamgui`, so any change to that code moves
  it: `main` at 3249d67 passed, and the bump's version-string change led it here.
- **Fix:** branch `test/echoed-password-surrounding-text`. `_usage_error` renders the error a third time
  with every value one character long. Scrubbed, that is the surrounding text, and a case where it holds a
  password is skipped. The error read for the message check is built from it too, so a scrub that leaked
  could no longer make the property skip its own leak. No product code changed.
- **Prevention:** `CRASHES` in `tests/test_fuzz_targets.py` keeps each input the fuzz job failed on (its
  log's `Base64:` line, with the property named), run by `test_a_past_fuzz_crash_holds`; this one fails
  on the old property. Checked: the new skip fires on 0 of 3,000 ordinary draws, and the property still
  fails with the scrub disabled and with a message that quotes argv.
