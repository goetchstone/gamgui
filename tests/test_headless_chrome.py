"""Every headless Chrome the repo starts must stay off the operator's login Keychain.

macOS Chrome with a fresh --user-data-dir asks the Keychain for its "Chrome Safe Storage" key, which
pops a Keychain prompt on the operator's screen for every screenshot or accessibility run
(docs/failure-log.md, 2026-09-24). --use-mock-keychain prevents it. A text scan: any tracked script
or test that starts Chrome headless must also pass that flag.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_every_headless_chrome_launch_uses_the_mock_keychain():
    tracked = subprocess.run(["git", "ls-files", "*.py", "*.sh", "*.js"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout.split()
    offenders = [
        f for f in tracked
        if f != "tests/test_headless_chrome.py"
        and "--headless" in (text := (ROOT / f).read_text(errors="replace"))
        and "--use-mock-keychain" not in text
    ]
    assert not offenders, f"headless Chrome without --use-mock-keychain (Keychain prompts): {offenders}"
