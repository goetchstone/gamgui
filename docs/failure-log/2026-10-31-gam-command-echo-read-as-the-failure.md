# 2026-10-31 — GAM's command echo and help lines read as the failure

- **Symptom:** found by SwiftGamGui while writing its mock's transfer failures (its failure log, 2026-10-13,
  "GAM's command echo read as the failure"), never seen live here. Reproduced on `main` with GAM's stderr
  shapes, read from the vendored 7.49.02 build:
  - A transfer to an owner who isn't a user read as `GAM failed (unknown, exit=56): Command: gam create
    datatransfer …`, with the unknown remediation, instead of "User: x, Does not exist".
  - New here, not in SwiftGamGui's entry: every usage error's message was GAM's last help line, "Help:
    Documentation is at https://github.com/GAM-team/GAM/wiki", not the error itself.
- **Cause:** `core/gam/errors.py`'s `_error_lines` skipped progress and the service-account instructions,
  but not GAM's framing of a refused command:
  - `entityDoesNotExistExit` and `usageErrorExit` first write `Cmd.CommandLineWithBadArgumentMarked`:
    "Command: gam … >>>bad<<< …".
  - `usageErrorExit` then writes `Msg.HELP_SYNTAX` and `Msg.HELP_WIKI`.
  - An echo read as unknown outranks not-found. The help lines are the last unknown lines, so they became
    the message.
- **Why not caught:** the mock lied twice.
  - Its `usage_error` printed no help lines, so its error line was always last.
  - Its transfer reported a missing owner as the per-entity warning (no echo, no "ERROR:"), which is not
    what `convertEmailAddressToUID` does.
  - The onboarding tests' own `echoing_gam` printed the echo *after* the error. They relied on the echo
    being the message to prove a password is masked.
- **Fix:** branch `fix/gam-command-echo`. `_FRAMING_LINE` (`Command: `, `Help: Syntax in file `, `Help:
  Documentation is at `) is skipped at the start of the stripped line, case-sensitively.
- **Mock changes:**
  - `usage_error` prints GAM's two help lines.
  - A new `does_not_exist_exit` prints the echo, a blank line and "ERROR: User: x, Does not exist", with exit
    code 56.
  - The transfer handler resolves each owner as GAM reads it. The `*missing*`/`*nonexistent*` trigger is
    kept; real GAM fails any address that isn't a user (backport plan A5).
  - `echoing_gam` prints GAM's order: the echo, then an error repeating the password's value, then the help
    lines. The by-value mask is still proven, now on the error line.
- **Prevention:**
  - `scripts/gam_rc_table.py` emits `framing`: the echo's text constants and every glmsgs message starting
    "Command" or "Help". `tests/test_gam_framing.py` holds that set (so a new help line or an error worded
    that way is read before a bump) and checks that lines built from them are skipped (required under
    `EXIT_CODES_REQUIRE_GAM`), and runs a missing transfer owner and a usage error through the mock.
  - `test_errors.py` pins both shapes, plus lookalikes that stay errors ("Command can not be nested.", an
    "ERROR: Command: …" line, lowercase).
  - The property tests' chatter draws the echo and the help lines.
  - With the skip removed 9 tests fail, and each partial or widened mutant fails its own test.
  - SwiftGamGui has the same help-line bug (its `isCommandEcho` skips only the echo); not fixed from here.
  - Not live-proven: a transfer to a missing owner on a real tenant is still owed (`live-verify`).
