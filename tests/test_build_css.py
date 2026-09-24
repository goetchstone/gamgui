"""scripts/build_css.sh runs the Tailwind CLI only when it matches a committed SHA-256 pin, and the
committed gamgui/web/static/app.css is what that pinned CLI builds.

The gate tests are hermetic, like tests/test_fetch_gam.py: the script is copied into a throwaway
root and driven with a stub `curl` that serves a fake CLI, so nothing here touches github.com, the
real build/tailwind/ cache or the real app.css.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "build_css.sh"
CHECKSUMS = REPO_ROOT / "scripts" / "tailwind_checksums.txt"
APP_CSS = REPO_ROOT / "gamgui" / "web" / "static" / "app.css"

VERSION = re.search(r'^TAILWIND_VERSION="(v[\d.]+)"$', SCRIPT.read_text(), re.M)[1]
ASSETS = re.findall(r'ASSET="(tailwindcss-[\w-]+)"', SCRIPT.read_text())
_SUPPORTED = (platform.system(), platform.machine()) in {
    ("Darwin", "arm64"), ("Darwin", "x86_64"), ("Linux", "x86_64"), ("Linux", "aarch64"), ("Linux", "arm64"),
}
needs_script = pytest.mark.skipif(
    not _SUPPORTED or shutil.which("shasum") is None, reason="build_css.sh needs a pinned platform and shasum",
)

FAKE_CSS = "/* fake tailwind */\n.a{}\n"
FAKE_CLI = (
    "#!/bin/sh\n"
    "out=''\n"
    'while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift 2 ;; *) shift ;; esac; done\n'
    "printf '/* fake tailwind */\\n.a{}\\n' > \"$out\"\n"
)
FAKE_SHA = hashlib.sha256(FAKE_CLI.encode()).hexdigest()


# --- hermetic harness --------------------------------------------------------------------

def _make_root(tmp_path: Path, pin: str | None = FAKE_SHA) -> Path:
    """A fake repo root around a copy of the script. `pin` is the hash committed for every asset."""
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    (root / "gamgui" / "web" / "static").mkdir(parents=True)
    shutil.copy2(SCRIPT, root / "scripts" / "build_css.sh")
    (root / "tailwind.config.js").write_text("module.exports = {};\n")
    (root / "gamgui" / "web" / "tailwind.css").write_text("@tailwind utilities;\n")

    (tmp_path / "fake-cli").write_text(FAKE_CLI)
    lines = ["# pins\n"] + ([f"{pin}  {VERSION}/{a}\n" for a in ASSETS] if pin else [])
    (root / "scripts" / "tailwind_checksums.txt").write_text("".join(lines))
    return root


def _run(root: Path, tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        "out=''; url=''\n"
        "while [ $# -gt 0 ]; do\n"
        '  case "$1" in -o) out="$2"; shift 2 ;; -*) shift ;; *) url="$1"; shift ;; esac\n'
        "done\n"
        'echo "$url" >> "$STUB_CURL_LOG"\n'
        'cp "$STUB_ASSET" "$out"\n'
    )
    curl.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "STUB_ASSET": str(tmp_path / "fake-cli"),
        "STUB_CURL_LOG": str(tmp_path / "curl.log"),
    }
    return subprocess.run(
        [str(root / "scripts" / "build_css.sh"), *args], capture_output=True, text=True, env=env, timeout=60,
    )


def _fetches(tmp_path: Path) -> list[str]:
    log = tmp_path / "curl.log"
    return log.read_text().split() if log.exists() else []


def _cached(root: Path) -> list[Path]:
    return sorted((root / "build" / "tailwind").rglob("tailwindcss-*"))


def _css(root: Path) -> Path:
    return root / "gamgui" / "web" / "static" / "app.css"


# --- the gate ----------------------------------------------------------------------------

@needs_script
def test_unpinned_asset_is_refused_before_any_download(tmp_path):
    root = _make_root(tmp_path, pin=None)
    r = _run(root, tmp_path)

    assert r.returncode == 1, r.stdout + r.stderr
    assert "no pinned checksum" in r.stderr and "refusing" in r.stderr
    assert _fetches(tmp_path) == [] and _cached(root) == [] and not _css(root).exists()


@needs_script
def test_checksum_mismatch_refuses_and_caches_nothing(tmp_path):
    root = _make_root(tmp_path, pin="0" * 64)
    r = _run(root, tmp_path)

    assert r.returncode == 1, r.stdout + r.stderr
    assert "checksum mismatch" in r.stderr
    assert _cached(root) == [] and not _css(root).exists()


@needs_script
def test_matching_pin_fetches_from_the_release_and_builds(tmp_path):
    root = _make_root(tmp_path)
    r = _run(root, tmp_path)

    assert r.returncode == 0, r.stdout + r.stderr
    assert _css(root).read_text() == FAKE_CSS
    [url] = _fetches(tmp_path)
    assert re.fullmatch(
        rf"https://github\.com/tailwindlabs/tailwindcss/releases/download/{re.escape(VERSION)}/tailwindcss-[\w-]+", url)
    [cli] = _cached(root)
    assert cli.name == url.rsplit("/", 1)[1] and cli.parent.name == VERSION

    # A second run uses the cache.
    assert _run(root, tmp_path).returncode == 0 and len(_fetches(tmp_path)) == 1


@needs_script
def test_check_fails_on_a_stale_css_and_never_writes(tmp_path):
    root = _make_root(tmp_path)
    assert _run(root, tmp_path).returncode == 0

    assert _run(root, tmp_path, "--check").returncode == 0
    _css(root).write_text(FAKE_CSS + ".added-by-hand{}\n")
    r = _run(root, tmp_path, "--check")
    assert r.returncode == 1 and "stale" in r.stderr and "make css" in r.stderr
    assert _css(root).read_text().endswith(".added-by-hand{}\n")


@needs_script
def test_a_tampered_cached_cli_is_discarded_not_run(tmp_path):
    root = _make_root(tmp_path)
    assert _run(root, tmp_path).returncode == 0
    [cli] = _cached(root)
    cli.write_text("#!/bin/sh\necho pwned > \"$(dirname \"$0\")/ran\"\n")

    r = _run(root, tmp_path, "--no-fetch")
    assert r.returncode == 3 and "does not match its pin" in r.stderr
    assert not cli.exists() and not (cli.parent / "ran").exists()

    r = _run(root, tmp_path)   # with fetching allowed it re-downloads the pinned binary
    assert r.returncode == 0, r.stdout + r.stderr
    assert hashlib.sha256(cli.read_bytes()).hexdigest() == FAKE_SHA


@needs_script
def test_no_fetch_never_downloads(tmp_path):
    root = _make_root(tmp_path)
    r = _run(root, tmp_path, "--no-fetch")
    assert r.returncode == 3 and _fetches(tmp_path) == []


@needs_script
def test_unknown_flag_rejected(tmp_path):
    root = _make_root(tmp_path, pin=None)
    assert _run(root, tmp_path, "--chek").returncode == 2


# --- the committed pins and CSS -----------------------------------------------------------

def test_every_platform_the_script_maps_is_pinned():
    pins = dict(
        (name, sha) for sha, name in
        (line.split() for line in CHECKSUMS.read_text().splitlines() if line.strip() and not line.startswith("#"))
    )
    assert set(ASSETS) >= {"tailwindcss-macos-arm64", "tailwindcss-macos-x64", "tailwindcss-linux-x64"}
    for asset in ASSETS:
        assert re.fullmatch(r"[0-9a-f]{64}", pins.get(f"{VERSION}/{asset}", "")), asset


def test_committed_css_was_built_by_the_pinned_cli():
    # The CLI's own banner, so a TAILWIND_VERSION bump without `make css` fails here too.
    assert f"/* ! tailwindcss {VERSION} | MIT License" in APP_CSS.read_text()


@pytest.mark.skipif(not _SUPPORTED, reason="no pinned CLI for this platform")
def test_committed_css_is_current():
    """What CI's lint job runs, here only when `make css` has already cached the pinned CLI."""
    r = subprocess.run([str(SCRIPT), "--check", "--no-fetch"], capture_output=True, text=True, timeout=120)
    if r.returncode == 3:
        pytest.skip("the pinned Tailwind CLI isn't cached (make css fetches it)")
    assert r.returncode == 0, r.stdout + r.stderr


def test_ci_checks_the_committed_css():
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "run: ./scripts/build_css.sh --check\n" in ci


def test_build_app_rebuilds_the_css_the_spec_bundles():
    build = (REPO_ROOT / "scripts" / "build_app.sh").read_text()
    assert build.index("./scripts/build_css.sh\n") < build.index("-m PyInstaller")
    spec = (REPO_ROOT / "gamgui.spec").read_text()
    assert '("gamgui/web/static", "gamgui/web/static")' in spec
