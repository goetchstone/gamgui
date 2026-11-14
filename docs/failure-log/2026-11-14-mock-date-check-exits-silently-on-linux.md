# 2026-11-14 — The mock's event-date check exited silently on Linux CI

- **Symptom:** PR 125's three Ubuntu test jobs failed on
  `test_events_with_a_date_gam_cannot_read_is_its_usage_error[2026-02-30-…]`. The mock exited 1 with no
  stderr, where the test (and GAM) expect usage error 2, "Invalid argument: Expected <yyyy-mm-dd>". macOS
  was green.
- **Cause:** `event_epoch` in `tests/fixtures/mock_gam.sh` assigned `back=$(date … || date -u -d …)`. The mock
  runs under `#!/bin/sh` with `set -eu`. On Linux `/bin/sh` is dash, and dash exits the whole script when an
  assignment from a failed `$(...)` fails, even inside a function called from another `$(...)`. GNU date
  refuses Feb 30 (which is the point of that line), so the mock died before it could print GAM's error. On
  macOS, BSD date rolls Feb 30 over and succeeds, so the assignment never failed there.
- **Why not caught:** the fix in PR 125 added the date check and was run only on macOS. Its comment named
  the GNU path, but nothing ran it before CI.
- **Fix:** `|| true` on that assignment and on the function's final `date`, so a refused date gives an
  empty result and the caller prints GAM's usage error. Reproduced and proved in a Debian
  `python:3.12-slim` container: `tests/test_mock_gam_shapes.py` gives 1 failed / 75 passed with the old mock
  and 76 passed with the fix.
- **Prevention:** the Ubuntu CI jobs are the tripwire, and they caught this. Lesson for the mock: under
  `set -e`, any `x=$(cmd)` whose `cmd` is *meant* to fail on some platform needs `|| true`. Dash and
  macOS's sh differ here, so a mock change that branches on platform needs a Linux run (Docker
  `python:3.12-slim`) before it is pushed.
