# 2026-09-24 — Headless Chrome runs popped Keychain prompts on the operator's screen

- **Symptom:** while agents verified UI work (screenshots, CSP and accessibility checks), the operator
  kept getting macOS Keychain prompts from Google Chrome — "Chrome Safe Storage".
- **Cause:** every headless Chrome was started with a fresh `--user-data-dir`; macOS Chrome then asks
  the login Keychain for its Safe Storage key to encrypt that throwaway profile's cookies.
  `scripts/readme_screenshots.py` — the pattern the agents were told to reuse — didn't pass
  `--use-mock-keychain`.
- **Why not caught:** the runs worked (a denied prompt doesn't stop them), and the prompt appears on
  the operator's screen, not in any output an agent sees.
- **Fix:** `CHROME_FLAGS` in `scripts/readme_screenshots.py` adds `--use-mock-keychain` (plus
  `--no-default-browser-check`); agents launching their own Chrome were told the same.
- **Prevention:** `tests/test_headless_chrome.py` fails on any tracked script or test that starts
  Chrome `--headless` without `--use-mock-keychain`. Nothing read the operator's real Chrome profile:
  each run used its own temp profile.
