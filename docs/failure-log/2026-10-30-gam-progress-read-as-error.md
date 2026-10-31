# 2026-10-30 — GAM's progress read as the error: a per-entity "Getting …" line hid the real failure

- **Symptom:** found by SwiftGamGui while building its calendar reads (its failure log, 2026-10-13, "GAM's
  progress read as an error"), never seen live here. Reproduced on `main` with GAM's stderr shapes:
  - A refused `report users` read as `GAM failed (unknown, exit=61): Getting Reports for all users`, with
    the generic remediation, instead of the permission refusal GAM printed after it.
  - A `deprovision` whose sign-out Google refused kept its kind, but `kinds` also held `UNKNOWN`, which a
    best-effort caller (`_run_write`'s `kinds ⊆ tolerate_kinds`) refuses.
  - Any read that names its entities (`print groups` for given groups, the calendar reads) printed
    "Getting Group x (1/3)", so a benign not-found beside it read as unknown.
- **Cause:** with `show_gettings` on (GAM's default), GAM says on stderr what it is getting, before each
  entity. `core/gam/errors.py`'s `_PROGRESS_LINE` was `(Getting all |Got \d+ )`, which covers two of
  GAM's six printers. `printGettingEntityItem` ("Getting Group sales@example.com (1/3)") and
  `printGettingEntityItemForWhom` ("Getting Reports for all users", "Getting Access Tokens for alice@…")
  were read as error lines. Unknown outranks not-found, permission-denied and the per-user refusals, so
  the real failure lost, and the progress line became the message.
- **Why not caught:** the mock lied. It printed the report's progress on **stdout**, in words GAM never
  writes ("Getting Reports for the customer"), and nothing at all for `deprovision`. The property tests'
  chatter only ever used "Getting all …" and "Got N …". So no test put a per-entity "Getting …" line on
  stderr.
- **Fix:** branch `fix/gam-progress-lines`. `_PROGRESS_LINE` is `(Getting |Got \d+ )`, matched at the
  start of the stripped line and case-sensitively, as SwiftGamGui's `isProgress` reads it. That is safe
  because the build's only `gamlib.glmsgs` messages starting "Getting" or "Got" are `GETTING`,
  `GETTING_ALL` and `GOT`, read from the vendored 7.49.02 build. GAM's errors start "ERROR:", "WARNING:"
  or an entity's name.
- **Mock changes:**
  - `report users` prints GAM's progress on stderr: the "Getting" line, then the page count after a CR,
    ended with CR LF.
  - A `noreports` state marker refuses the report with accessErrorExit's "Access Forbidden" line and
    `INVALID_DOMAIN_RC` (61). Both are checked against the build's glmsgs and `*_RC` table.
  - `deprovision` prints its three "Getting … for <user>" lines on stderr. Their entity words are checked
    against the build's glentity.
- **Prevention:**
  - `scripts/gam_rc_table.py` emits `progress`: every glmsgs message starting "Getting" or "Got".
    `tests/test_gam_progress.py` holds it to the three words, so an error GAM words that way is read before
    a bump is trusted (required under `EXIT_CODES_REQUIRE_GAM`).
  - The same file runs the refused report and a deprovision with a refused sign-out through the mock.
  - `test_errors.py` pins each printer's shape before a real failure, plus lookalikes that stay errors.
  - The property tests' `_CHATTER` now draws every printer's shape, with and without the counter.
  - With the old pattern, 7 tests fail. Case-insensitive, anywhere-in-line and no-space mutants each fail
    their lookalike.
  - Not live-proven: a refused report or a deprovision on a real tenant is still owed (`live-verify`).
