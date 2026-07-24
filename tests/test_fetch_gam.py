"""scripts/fetch_gam.sh must fail CLOSED on an asset with no committed SHA-256 pin.

The asset name comes out of the release JSON the script just downloaded, so "this name isn't in
gam_checksums.txt" is precisely what renaming an asset produces — installing it anyway would make
the pin decorative. Everything here runs offline: the script is copied into a throwaway root and
driven with a stub `curl` on PATH, so no test ever touches api.github.com or the real
gamgui/resources/gam7.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "fetch_gam.sh"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# The script maps uname -m onto GAM's asset naming; anything else exits before the checksum gate.
_ARCH = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x86_64"}.get(platform.machine())
ASSET = f"gam-9.99.99-macos26.4-{_ARCH}.tar.xz"

# `find -perm +111` (the executable-lookup after extraction) is BSD-only, and this is a macOS-only
# vendoring script — on Linux we only assert on the gate itself, not on a completed install.
_INSTALLS = sys.platform == "darwin"

pytestmark = pytest.mark.skipif(
    _ARCH is None or shutil.which("shasum") is None,
    reason="fetch_gam.sh needs a macOS-style arch and shasum",
)


# --- hermetic harness --------------------------------------------------------------------

def _fake_tarball(path: Path) -> str:
    """A real .tar.xz holding an executable `gam`, so the install path can run to completion."""
    gam = io.BytesIO(b'#!/bin/sh\necho "GAM 9.99.99"\n')
    with tarfile.open(path, "w:xz") as tf:
        info = tarfile.TarInfo("gam-9.99.99/gam")
        info.size = len(gam.getvalue())
        info.mode = 0o755
        tf.addfile(info, gam)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stub_curl(bin_dir: Path) -> None:
    """Serve both curl calls (release JSON, then the asset) from local files instead of the net."""
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        "out=''; url=''\n"
        "while [ $# -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    -o) out=\"$2\"; shift 2 ;;\n"
        "    -H) shift 2 ;;\n"
        "    -*) shift ;;\n"
        "    *) url=\"$1\"; shift ;;\n"
        "  esac\n"
        "done\n"
        "case \"$url\" in\n"
        "  *api.github.com*) cp \"$STUB_RELEASE_JSON\" \"$out\" ;;\n"
        "  *) cp \"$STUB_ASSET\" \"$out\" ;;\n"
        "esac\n"
    )
    curl.chmod(0o755)


def _make_root(tmp_path: Path, pinned: str | None) -> tuple[Path, str]:
    """Build a fake repo root around a copy of the script. `pinned` is the hash to commit, if any."""
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, root / "scripts" / "fetch_gam.sh")

    sha = _fake_tarball(tmp_path / "asset.tar.xz")
    (tmp_path / "release.json").write_text(
        '{"tag_name": "v9.99.99", "assets": ['
        f'{{"name": "{ASSET}", "browser_download_url": "https://example.invalid/{ASSET}"}}]}}'
    )
    lines = ["# Pinned SHA-256 checksums.\n"]
    if pinned is not None:
        lines.append(f"{pinned}  {ASSET}\n")
    (root / "scripts" / "gam_checksums.txt").write_text("".join(lines))
    return root, sha


def _run(root: Path, tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _stub_curl(bin_dir)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "STUB_RELEASE_JSON": str(tmp_path / "release.json"),
        "STUB_ASSET": str(tmp_path / "asset.tar.xz"),
    }
    return subprocess.run(
        [str(root / "scripts" / "fetch_gam.sh"), *args],
        capture_output=True, text=True, env=env, timeout=60,
    )


def _dest(root: Path) -> Path:
    return root / "gamgui" / "resources" / "gam7"


# --- the gate ----------------------------------------------------------------------------

def test_unpinned_asset_is_refused_by_default(tmp_path):
    root, sha = _make_root(tmp_path, pinned=None)
    r = _run(root, tmp_path)

    assert r.returncode == 1, r.stdout + r.stderr
    assert "no pinned checksum" in r.stderr and "refusing to install" in r.stderr
    # The maintainer gets the exact line to commit, plus the escape hatch.
    assert f"{sha}  {ASSET}" in r.stderr
    assert "--allow-unpinned" in r.stderr
    assert not _dest(root).exists(), "refused install must not touch resources/gam7"


def test_unpinned_asset_installs_only_with_the_opt_in(tmp_path):
    root, sha = _make_root(tmp_path, pinned=None)
    r = _run(root, tmp_path, "--allow-unpinned")

    assert "WARNING" in r.stderr and "UNVERIFIED" in r.stderr
    assert f"{sha}  {ASSET}" in r.stderr
    assert "==> Extracting..." in r.stdout, r.stdout + r.stderr
    if _INSTALLS:
        assert r.returncode == 0, r.stdout + r.stderr
        assert (_dest(root) / "gam").exists()
        assert (_dest(root) / "SHA256").read_text().strip() == f"{sha}  {ASSET}"


def test_matching_pin_installs_without_the_flag(tmp_path):
    root, sha = _make_root(tmp_path, pinned=None)
    (root / "scripts" / "gam_checksums.txt").write_text(f"{sha}  {ASSET}\n")
    r = _run(root, tmp_path)

    assert "Checksum verified" in r.stdout, r.stdout + r.stderr
    assert "WARNING" not in r.stderr
    if _INSTALLS:
        assert r.returncode == 0, r.stdout + r.stderr
        assert (_dest(root) / "VERSION").read_text().strip() == "v9.99.99"


@pytest.mark.parametrize("args", [(), ("--allow-unpinned",)])
def test_checksum_mismatch_always_refuses(tmp_path, args):
    root, _sha = _make_root(tmp_path, pinned="0" * 64)
    r = _run(root, tmp_path, *args)

    assert r.returncode == 1, r.stdout + r.stderr
    assert "checksum mismatch" in r.stderr
    assert not _dest(root).exists()


def test_unknown_flag_still_rejected(tmp_path):
    root, _sha = _make_root(tmp_path, pinned=None)
    r = _run(root, tmp_path, "--allow-unpinned-please")
    assert r.returncode == 2 and "unknown arg" in r.stderr


# --- static checks (also meaningful where the script can't run) ---------------------------

def test_script_parses_allow_unpinned_and_defaults_it_off():
    text = SCRIPT.read_text()
    assert "--allow-unpinned) ALLOW_UNPINNED=1" in text
    assert "ALLOW_UNPINNED=0" in text


def test_unpinned_branch_exits_nonzero():
    """The fail-open `else` branch must end in `exit 1`, not fall through to the install."""
    text = SCRIPT.read_text()
    gate = text.split('elif [ "$ALLOW_UNPINNED" -eq 1 ]', 1)[1].split("\nfi\n", 1)[0]
    refuse = gate.split("\nelse\n", 1)[1]
    assert "exit 1" in refuse


def test_ci_only_grants_the_opt_in_to_the_preview_job():
    text = CI_WORKFLOW.read_text()
    assert "./scripts/fetch_gam.sh --tag latest --allow-unpinned" in text
    # The pinned gam-compat job must keep failing closed.
    assert "run: ./scripts/fetch_gam.sh\n" in text


def test_pywebview_is_pinned():
    # Matched textually rather than with a TOML parser: tomllib is 3.11+, and the supported floor
    # is 3.10. pywebview hosts the WKWebView, so an unpinned floor would pull an unreviewed
    # version into the shipped .app.
    text = (REPO_ROOT / "pyproject.toml").read_text()
    assert re.search(r'^\s*"pywebview==[\d.]+"', text, re.MULTILINE), text

    # build_app.sh is the path that actually builds the bundle, so it must pin too.
    build = (REPO_ROOT / "scripts" / "build_app.sh").read_text()
    assert "pywebview>=" not in build, build
    assert re.search(r'"pywebview==[\d.]+"', build), build
