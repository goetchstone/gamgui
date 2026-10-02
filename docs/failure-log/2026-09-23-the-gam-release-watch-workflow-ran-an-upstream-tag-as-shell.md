# 2026-09-23 — The GAM release-watch workflow ran an upstream tag as shell code

- **Symptom:** a review found `.github/workflows/gam-watch.yml` expanding
  `${{ steps.check.outputs.latest }}` — the GAM-team/GAM latest release tag — straight into a
  `run:` script, in a job with `contents: write` + `pull-requests: write`, before the attestation
  check. A legal tag such as `v7.49.0$(curl …|sh)` would have executed on the runner. Not exploited.
- **Cause:** GitHub substitutes `${{ }}` into the script text before the shell parses it; the value
  was treated as a trusted version string because it came from the upstream we pin.
- **Why not caught:** nothing looked at workflow files except `test_fetch_gam.py`'s check of the
  `--allow-unpinned` flag; the later "Open the PR" step already used `env:`, so the pattern was
  known but not enforced.
- **Fix:** the tag reaches `bump_gam.py` via `env: LATEST`, and the check step refuses any tag not
  matching `^[0-9]+(\.[0-9]+){2,3}$` (bash `=~`, which anchors the whole string, newlines included)
  before it is written to `$GITHUB_OUTPUT` or used anywhere.
- **Prevention:** `tests/test_workflow_safety.py` fails on any `${{` inside a `run:` block of any
  workflow (plus a self-test that the scanner catches the original shape). The regex was checked
  under bash 3.2 against `$(…)`, an embedded newline, a two-part version and a `v` prefix.
