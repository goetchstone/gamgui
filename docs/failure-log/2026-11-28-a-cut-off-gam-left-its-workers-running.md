# 2026-11-28 — A cut-off gam left its worker processes running, credentials loaded, and the app waited them out

- **Symptom:** found reading GAM's source for the scale work (`MultiprocessGAMCommands`, `doAutoBatch`); never seen live.
  When the runner cut off a gam call, on a timeout or a cancel (the app quitting), `_stop` killed the parent process only.
  GAM's parallel loops fork a pool of workers, each of which loads the credentials: `csv` (5.4's fast signature write),
  `batch`, and auto-batch. Those workers ran on with their tokens in memory while EphemeralConfig wiped the directory
  under them, and could keep making calls. They also hold gam's stdout and stderr open, so `communicate()` waited for them.
  A "timed out" run therefore went on to its end: the timeout didn't stop anything. The stand-in test took 30 s, its
  worker's whole life, where it should have taken 1 s.
- **Cause:** the runner was written for one gam process per call, and `proc.kill()` signals one PID. Nothing started gam
  in a session or process group of its own, so its workers couldn't be signalled together.
- **Why not caught:** the mock runs one shell process per call, and its `csv -` loop (`mock_gam_loops.py`) runs each row
  in turn, not a pool. No test cut off a gam call that had children.
- **Fix:** `_exec` starts gam with `start_new_session=True`, so gam and every worker share one process group, and `_stop`
  sends SIGKILL to the whole group (`os.killpg`) before reaping the parent. A cut-off now returns at once and leaves no
  process behind.
- **Prevention:** `tests/test_runner_stops_workers.py` runs a stand-in gam that forks a long-lived worker, then cuts it off
  by timeout and by cancel. It requires the call to return within 10 s and the worker to be gone. Both failed before the
  fix (30 s each). Not covered: Windows, which isn't a target.
