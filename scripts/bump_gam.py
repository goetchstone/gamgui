#!/usr/bin/env python3
"""Bump the vendored GAM to a target version — the whole "Updating GAM" runbook as one command.

The mechanical steps a maintainer used to do by hand, in order:

  1. download the release asset and **verify GitHub's build attestation** for it
     (`gh attestation verify --repo GAM-team/GAM`) — this is the trust anchor that lets the pin be
     written automatically without becoming trust-on-first-use: we are not trusting "whatever we
     downloaded", we are trusting a Sigstore-signed provenance statement that GAM-team's CI built it;
  2. write that asset's SHA-256 into `scripts/gam_checksums.txt`;
  3. re-vendor through `scripts/fetch_gam.sh`, which now verifies the download against that pin;
  4. bump `EXPECTED_GAM_VERSION` and `TAG`;
  5. point the test mock's `gam version` at the new number (the mock must match the real GAM);
  6. regenerate the browse catalog;
  7. refresh the command counts stated in `CLAUDE.md` (a test guards them).

What is deliberately NOT here — because it needs judgment or a real tenant, not mechanism:
  - reading `GamUpdate.txt` for breaking changes (the contract test catches a renamed/removed command
    we use, and turns it into a red suite rather than a silent break);
  - `scripts/acceptance.py` against a live tenant (the only true output-shape check).

So: run this, run `pytest`, then a human reviews and does the live pass before shipping. In CI this
runs on a schedule and opens a PR; locally a maintainer runs `python scripts/bump_gam.py vX.Y.Z`.

Usage:  python scripts/bump_gam.py vX.Y.Z  [--allow-unattested]
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "GAM-team/GAM"
CHECKSUMS = ROOT / "scripts" / "gam_checksums.txt"
FETCH = ROOT / "scripts" / "fetch_gam.sh"
COMMANDS_PY = ROOT / "gamgui" / "core" / "gam" / "commands.py"
MOCK = ROOT / "tests" / "fixtures" / "mock_gam.sh"
CLAUDE_MD = ROOT / "CLAUDE.md"


# --- pure text transforms (unit-tested; no network) ----------------------------------------------

def select_asset(release: dict, arch: str) -> "tuple[str, str]":
    """`(asset_name, download_url)` for the macOS asset, mirroring fetch_gam.sh: highest ``macosNN``.

    A release ships several ``macosNN`` builds; both this and fetch_gam.sh pick the highest, so the
    pin is deterministic per release rather than per whichever runner or Mac did the bump.
    """
    cands = []
    for a in release.get("assets", []):
        name = a["name"]
        if "macos" in name and arch in name and name.endswith(".tar.xz"):
            m = re.search(r"macos(\d+)", name)
            cands.append((int(m.group(1)) if m else 0, name, a["browser_download_url"]))
    if not cands:
        raise SystemExit(f"no macOS/{arch} .tar.xz asset in the release")
    cands.sort()
    _, name, url = cands[-1]
    return name, url


def bump_version_strings(version: str) -> None:
    """Set EXPECTED_GAM_VERSION (bare) in commands.py and TAG (``v``-prefixed) in fetch_gam.sh."""
    v = version.lstrip("v")
    _sub(COMMANDS_PY, r'EXPECTED_GAM_VERSION = "[0-9.]+"', f'EXPECTED_GAM_VERSION = "{v}"')
    _sub(FETCH, r'^TAG="v[0-9.]+"', f'TAG="v{v}"', flags=re.MULTILINE)


def bump_mock_version(version: str) -> None:
    """The mock's `gam version` must report the vendored version, or the drift guards fail — by design."""
    v = version.lstrip("v")
    _sub(MOCK, r'echo "GAM [0-9.]+ - mock"', f'echo "GAM {v} - mock"')


def refresh_claude_counts(total: int, buildable: int, curated: int, promoted: int, version: str) -> None:
    """Rewrite the catalog counts + pinned version stated in CLAUDE.md so its guard test stays green."""
    v = version.lstrip("v")
    _sub(CLAUDE_MD,
         r"Of \d+ catalog entries, \d+ run:\n   26 hand-curated \(the only ones that can \*change\* anything\) plus \d+ grammar-derived commands",
         (f"Of {total} catalog entries, {buildable} run:\n"
          f"   {curated} hand-curated (the only ones that can *change* anything) plus {promoted} "
          "grammar-derived commands"))
    _sub(CLAUDE_MD, r"\(currently [0-9.]+\)", f"(currently {v})")


def catalog_counts() -> "tuple[int, int, int, int]":
    """`(total, buildable, curated, promoted)` from the live in-memory catalog."""
    sys.path.insert(0, str(ROOT))
    from gamgui.core.catalog.catalog import load_catalog

    cmds = list(load_catalog().commands)
    buildable = [c for c in cmds if getattr(c, "buildable", False)]
    curated = [c for c in buildable if not str(getattr(c, "id", "")).startswith("raw.")]
    return len(cmds), len(buildable), len(curated), len(buildable) - len(curated)


def _sub(path: Path, pattern: str, repl: str, flags: int = 0) -> None:
    text = path.read_text()
    new, n = re.subn(pattern, repl, text, flags=flags)
    if n == 0:
        raise SystemExit(f"{path.name}: pattern not found, refusing to guess: {pattern!r}")
    path.write_text(new)


# --- orchestration (network + subprocess; not unit-tested) ---------------------------------------

def _arch() -> str:
    m = platform.machine().lower()
    return "arm64" if m in ("arm64", "aarch64") else "x86_64"


def main() -> int:
    ap = argparse.ArgumentParser(description="Bump the vendored GAM to a target version.")
    ap.add_argument("version", help="e.g. v7.47.02")
    ap.add_argument("--allow-unattested", action="store_true",
                    help="skip the GitHub build-attestation check (NOT for a real build — see the pin invariant)")
    args = ap.parse_args()
    version = args.version if args.version.startswith("v") else f"v{args.version}"

    rel = json.loads(_run(["gh", "api", f"repos/{REPO}/releases/tags/{version}"]))
    asset, url = select_asset(rel, _arch())
    print(f"==> {asset}")

    with tempfile.TemporaryDirectory() as td:
        blob = Path(td) / asset
        print("==> downloading…")
        urllib.request.urlretrieve(url, blob)  # noqa: S310 — GitHub release URL from the API above

        if args.allow_unattested:
            print("!! WARNING: skipping attestation — the pin will be trust-on-first-use.")
        else:
            print("==> verifying GitHub build attestation…")
            # Fail closed: no verified provenance -> no pin, no PR. This is what makes writing the pin
            # automatically safe. `gh attestation verify` exits non-zero if the bundle is missing or
            # does not chain to GAM-team/GAM's CI identity.
            _run(["gh", "attestation", "verify", str(blob), "--repo", REPO])
            print("    attestation OK.")

        sha = _sha256(blob)

    _write_pin(asset, sha)
    print(f"==> pinned {sha}  {asset}")

    # Re-vendor through the normal path, which re-verifies the download against the pin just written.
    _run(["bash", str(FETCH), "--tag", version], cwd=ROOT, stream=True)

    bump_version_strings(version)
    bump_mock_version(version)
    _run([sys.executable, str(ROOT / "scripts" / "build_command_catalog.py")], cwd=ROOT, stream=True)
    refresh_claude_counts(*catalog_counts(), version=version)

    print(f"\n==> bumped to {version}. Now run:  .venv/bin/python -m pytest -q")
    print("    then skim gamgui/resources/gam7/GamUpdate.txt and run scripts/acceptance.py on a tenant.")
    return 0


def _write_pin(asset: str, sha: str) -> None:
    lines = [ln for ln in CHECKSUMS.read_text().splitlines()
             if ln.startswith("#") or (ln.strip() and not ln.split()[1].startswith("gam-"))]
    lines.append(f"{sha}  {asset}")
    CHECKSUMS.write_text("\n".join(lines) + "\n")


def _sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _run(cmd: list, cwd: "Path | None" = None, stream: bool = False) -> str:
    if stream:
        subprocess.run(cmd, cwd=cwd, check=True)
        return ""
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout


if __name__ == "__main__":
    raise SystemExit(main())
