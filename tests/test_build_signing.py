"""The .app is signed with the hardened runtime, and its entitlements can't reopen env-var injection.

Static checks (no build, no signing). The hardened runtime is what makes dyld ignore a same-user
``launchctl setenv DYLD_INSERT_LIBRARIES=…`` aimed at the app the Keychain trusts, or at the ``gam``
that holds the plaintext credentials.
"""

from __future__ import annotations

import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
VENDORED_GAM = ROOT / "gamgui" / "resources" / "gam7" / "gam"

DLV = "com.apple.security.cs.disable-library-validation"
# Any of these would hand back what the hardened runtime takes away.
REOPENERS = {
    "com.apple.security.cs.allow-dyld-environment-variables",  # DYLD_INSERT_LIBRARIES works again
    "com.apple.security.get-task-allow",                       # any same-user process may attach
    "com.apple.security.cs.debugger",
    "com.apple.security.cs.disable-executable-page-protection",
}


def _entitlements(name: str) -> dict:
    return plistlib.loads((SCRIPTS / name).read_bytes())


def test_every_codesign_uses_the_hardened_runtime():
    sign = (SCRIPTS / "sign_app.sh").read_text()
    assert re.search(r"^sign\(\) \{ codesign --force --options runtime --sign ", sign, re.M)
    raw = [ln for ln in sign.splitlines() if re.match(r"\s*codesign\b", ln) and "--verify" not in ln]
    assert raw == []  # every signing call goes through sign()
    # The deep sign re-signs gam with the app's options, so gam must be signed after it, then resealed.
    order = [m.group(1) for m in re.finditer(r"^sign (.*)$", sign, re.M)]
    assert order == [
        '--deep --entitlements "$HERE/app.entitlements" "$APP"',
        '--entitlements "$HERE/gam.entitlements" "$GAM_BIN"',
        '--entitlements "$HERE/app.entitlements" "$APP"',
    ]
    build = (SCRIPTS / "build_app.sh").read_text()
    assert "./scripts/sign_app.sh" in build
    assert not re.search(r"^\s*codesign .*--sign", build, re.M)  # no signing path that skips it


def test_app_entitlements_are_library_validation_only():
    # A self-signed/ad-hoc signature has no Team ID, so without this every bundled dylib is refused.
    # Nothing else was needed (sign_app.sh records what was checked) — widen only with a reason.
    assert _entitlements("app.entitlements") == {DLV: True}


def test_no_entitlements_reopen_injection_or_debugging():
    for name in ("app.entitlements", "gam.entitlements"):
        assert not REOPENERS & set(_entitlements(name)), name


@pytest.mark.skipif(
    sys.platform != "darwin" or not VENDORED_GAM.exists() or not shutil.which("codesign"),
    reason="needs macOS and the vendored gam",
)
def test_gam_entitlements_match_the_vendored_upstream_binary():
    # We re-sign gam, replacing upstream's signature, so we carry upstream's entitlements. A GAM bump
    # that changes them fails here: review the difference, then update scripts/gam.entitlements.
    out = subprocess.run(
        ["codesign", "-d", "--entitlements", "-", "--xml", str(VENDORED_GAM)],
        capture_output=True, check=True,
    ).stdout
    assert _entitlements("gam.entitlements") == plistlib.loads(out)
