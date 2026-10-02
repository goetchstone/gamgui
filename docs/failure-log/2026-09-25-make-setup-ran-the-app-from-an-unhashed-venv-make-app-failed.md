# 2026-09-25 — `make setup` ran the app from an unhashed venv; `make app` failed on Python 3.10–3.12

- **Symptom:** (review 2: R1, R8) `make setup`, the README's start path (`make setup && make gam &&
  make run`), installed pyproject's flexible ranges and upgraded pip from the index with no hash
  checks, so the venv that loads the Keychain credentials when an operator runs from source was not
  the hash-locked set CI tests. Separately, `make app` stopped with pip's "no such option:
  --build-constraint" (exit 2) on a python.org 3.12.10 (bundled pip 25.0.1) or uv's 3.10.21 (pip
  23.0.1), though the script and README promise Python 3.10+.
- **Cause:** C3 (2026-09-24) moved CI and `build_app.sh` onto the locks but left `make setup` on
  `pip install -e ".[dev,desktop]"`; and `build_app.sh`'s new `--build-constraint` (pip 25.3+) ran
  with whatever pip `python -m venv` bundled — only 3.14's is new enough.
- **Why not caught:** `tests/test_locks.py` checked the install lines of CI and `build_app.sh` but
  not the Makefile, and CI's `app` job builds only with 3.14, whose pip has the flag.
- **Fix:** a third lock, `requirements/pip.txt` (`pip>=25.3`), installed first by `make setup` and
  `build_app.sh`; `make setup` then installs `dev.txt` + `app.txt` with `--require-hashes
  --build-constraint requirements/app.txt` and the project with `--no-deps --no-build-isolation`
  (hatchling and `editables` added to the dev lock for that); the flexible install moved to
  `make setup-latest`. Checked by `make setup` into scratch 3.10 and 3.12 venvs and `build_app.sh`'s
  pip steps into a fresh 3.12 build venv; the full PyInstaller build was not re-run.
- **Prevention:** `tests/test_locks.py::test_make_setup_installs_only_the_hash_locked_deps`,
  `test_only_make_setup_latest_installs_the_flexible_ranges`,
  `test_build_app_installs_only_the_app_lock_in_a_venv_of_its_own` (the locked pip first),
  `test_make_lock_regenerates_every_lock`, `test_dev_and_app_locks_agree_on_every_shared_pin`. No CI
  leg builds the `.app` on 3.10–3.12, so a new pip-only flag elsewhere would still get through.
