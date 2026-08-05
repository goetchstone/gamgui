"""The GAM-bump automation is only safe if its edits actually land. These guard the pure logic and,
crucially, that every pattern `scripts/bump_gam.py` rewrites still matches the file it targets — so a
future rename turns a real bump red here instead of silently no-op'ing in CI."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("bump_gam", ROOT / "scripts" / "bump_gam.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bump = _load()


def test_select_asset_picks_the_highest_macos_build():
    release = {"assets": [
        {"name": "gam-7.47.02-macos14.8-arm64.tar.xz", "browser_download_url": "u14"},
        {"name": "gam-7.47.02-macos26.5-arm64.tar.xz", "browser_download_url": "u26"},
        {"name": "gam-7.47.02-macos15.7-arm64.tar.xz", "browser_download_url": "u15"},
        {"name": "gam-7.47.02-linux-x86_64.tar.xz", "browser_download_url": "ulinux"},
    ]}
    name, url = bump.select_asset(release, "arm64")
    assert name == "gam-7.47.02-macos26.5-arm64.tar.xz" and url == "u26"


def test_select_asset_is_arch_specific():
    release = {"assets": [
        {"name": "gam-7.47.02-macos26.5-arm64.tar.xz", "browser_download_url": "arm"},
        {"name": "gam-7.47.02-macos26.5-x86_64.tar.xz", "browser_download_url": "intel"},
    ]}
    assert bump.select_asset(release, "x86_64")[1] == "intel"


def test_select_asset_raises_when_nothing_matches():
    with pytest.raises(SystemExit):
        bump.select_asset({"assets": [{"name": "gam-linux.tar.xz", "browser_download_url": "u"}]}, "arm64")


# Each tuple is (file, the exact regex bump_gam.py rewrites there). If GAM's version format or one of
# these files changes shape, the bump would SystemExit("pattern not found") in CI — so assert here.
_PATTERNS = [
    ("gamgui/core/gam/commands.py", r'EXPECTED_GAM_VERSION = "[0-9.]+"'),
    ("scripts/fetch_gam.sh", r'^TAG="v[0-9.]+"'),
    ("tests/fixtures/mock_gam.sh", r'echo "GAM [0-9.]+ - mock"'),
    ("CLAUDE.md", r"Of \d+ catalog entries, \d+ run:\n   26 hand-curated \(the only ones that can \*change\* anything\) plus \d+ grammar-derived commands"),
    ("CLAUDE.md", r"\(currently [0-9.]+\)"),
]


@pytest.mark.parametrize("relpath,pattern", _PATTERNS)
def test_bump_patterns_still_match_their_files(relpath, pattern):
    text = (ROOT / relpath).read_text()
    assert re.search(pattern, text, re.MULTILINE), f"{relpath}: bump_gam.py can no longer find {pattern!r}"


def test_write_pin_replaces_the_gam_line_and_keeps_the_header(tmp_path, monkeypatch):
    ck = tmp_path / "gam_checksums.txt"
    ck.write_text("# header comment\n"
                  "0000000000000000000000000000000000000000000000000000000000000000  gam-7.46.11-macosX-arm64.tar.xz\n")
    monkeypatch.setattr(bump, "CHECKSUMS", ck)
    bump._write_pin("gam-7.47.02-macos26.5-arm64.tar.xz", "a" * 64)
    lines = ck.read_text().splitlines()
    assert lines[0] == "# header comment"                       # header survives
    assert lines[-1] == f"{'a' * 64}  gam-7.47.02-macos26.5-arm64.tar.xz"
    assert not any("7.46.11" in ln for ln in lines)             # old pin gone, not appended-to
    assert sum(1 for ln in lines if ln and not ln.startswith("#")) == 1  # exactly one pin
