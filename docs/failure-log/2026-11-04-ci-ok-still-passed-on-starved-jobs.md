# 2026-11-04 — ci-ok still passed on jobs no runner picked up

- **Symptom:** `main`'s CI at 84e6885 (run 41658754078, attempt 1), which already held the fix of 2026-11-03
  (#73). `a11y` and macOS 3.14 were "not acquired by Runner of type hosted even after multiple attempts" and
  show `cancelled` in the job list. The run concluded `failure`, and `ci-ok`, the required check, passed
  with its gate step skipped. #73 had been reported as closing this gap and auto-merge as safe again; both
  were wrong. Nothing landed untested, because every merge in that stretch had its jobs checked by hand.
- **Cause:**
  - The diagnosis of 2026-11-03 (a step `if:` is ANDed with `success()`, which went false) was an inference
    from one run. With `always()` in front the step skipped anyway, so the condition itself was false:
    such a job matches none of failure, cancelled and skipped through `needs`. Its Checks conclusion is not
    its `needs` result. The value it reports is `abandoned`, first seen on run 41860860097 (below).
  - The gate listed bad values instead of requiring the good one, so any other value passed.
- **Why not caught:** the proof used a stand-in. #73 was checked on GitHub by cancelling a run by hand. Those
  jobs do report `cancelled`, a different path, and it says nothing about a job no runner picked up. The
  old condition was never run on a hand-cancelled run either, so its "before and after" compared two
  different situations. A job can't be made to miss a runner on demand, and that limit should have been
  stated rather than the gap reported closed. The workflow test only read the condition's text.
- **Fix:** branch `fix/ci-ok-job-results`. `scripts/ci_gate.py` requires "success" from every `needs` result
  and from every blocking job in the run's own job list, the data that showed the problem. Anything else
  fails, bad input included. Every step of `ci-ok` runs `if: always()`, and the gate prints each `needs`
  result, so the next such run shows the value.
- **Prevention:** `tests/test_ci_gate.py` runs the decision on the job lists of six real runs
  (`tests/fixtures/ci_runs.json`), including both runs with jobs no runner picked up, with every `needs`
  result claiming success. Any conclusion or result but "success" fails, and each piece of the decision
  was mutated to confirm a test fails without it (a job list check naming only failure and cancelled
  survived at first, so a test now holds that). `test_ci_ok_needs_every_blocking_job` holds the workflow.
  RULE-FEEDBACK has the stand-in lesson.
  **Seen on GitHub:** run 41860860097 (#81, 2026-11-04), one run so far. No runner picked up seven jobs. They
  show `cancelled` in the job list and report `needs.<job>.result = 'abandoned'`. The gate printed each result
  and exited 1, and `ci-ok` concluded `failure`.
