#!/usr/bin/env python3
"""Smoke-check a built GamGUI.app without launching it. CI's `app` job runs this after build_app.sh.

    build/venv/bin/python scripts/check_app.py dist/GamGUI.app

Run it with the build venv's Python: it reads the frozen archive with that venv's PyInstaller.
Checks the bundle's executable, the signature (verified, hardened runtime on the app and on gam),
the modules frozen into the archive, the data files where the frozen app looks for them, and that the
bundled gam starts (`gam version`, in a throwaway HOME/GAMCFGDIR, so no ~/.gam is read or created).
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Source modules the app never imports, so PyInstaller rightly leaves them out.
NOT_FROZEN = {
    "gamgui.core.connectors.abapit_connector",  # the optional second connector's seed (plan D5)
}
# Imported by name at runtime, where PyInstaller's static analysis can't follow: uvicorn resolves its
# loop/protocol/lifespan from config strings, keyring and pywebview pick a backend, Starlette parses
# forms only if python_multipart imports.
DYNAMIC = (
    "uvicorn.loops.auto", "uvicorn.protocols.http.auto", "uvicorn.lifespan.on",
    "keyring.backends.macOS", "webview.platforms.cocoa", "python_multipart",
    "objc", "AppKit", "WebKit", "fastapi", "starlette", "jinja2",
)


def pinned_gam_version() -> str:
    text = (ROOT / "gamgui" / "core" / "gam" / "commands.py").read_text()
    return re.search(r'^EXPECTED_GAM_VERSION = "([^"]+)"', text, re.M)[1]


def source_modules() -> set:
    mods = set()
    for p in (ROOT / "gamgui").rglob("*.py"):
        parts = list(p.relative_to(ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        mods.add(".".join(parts))
    return mods


def frozen_modules(exe: Path) -> tuple:
    from PyInstaller.archive.readers import CArchiveReader  # the build venv has it; tests don't need it

    pkg = CArchiveReader(str(exe))
    scripts = {name for name, entry in pkg.toc.items() if entry[-1] == "s"}
    pyz = next(name for name, entry in pkg.toc.items() if entry[-1] == "z")
    return scripts, set(pkg.open_embedded_archive(pyz).toc)


def hardened(path: Path) -> bool:
    out = subprocess.run(["codesign", "-d", "--verbose=1", str(path)], capture_output=True, text=True)
    flags = re.search(r"flags=0x[0-9a-f]+\(([^)]*)\)", out.stderr)
    return bool(flags) and "runtime" in flags[1].split(",")


def check(app: Path) -> list:
    problems = []
    pin = pinned_gam_version()
    contents = app / "Contents"
    exe = contents / "MacOS" / plistlib.loads((contents / "Info.plist").read_bytes())["CFBundleExecutable"]
    if not (exe.is_file() and os.access(exe, os.X_OK)):
        return [f"no executable at {exe}"]

    verify = subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], capture_output=True, text=True)
    if verify.returncode:
        problems.append("codesign --verify failed: " + verify.stderr.strip())

    scripts, modules = frozen_modules(exe)
    if "main" not in scripts:
        problems.append(f"entry script 'main' not in the archive (scripts: {sorted(scripts)})")
    missing = sorted(((source_modules() - NOT_FROZEN) | set(DYNAMIC)) - modules)
    if missing:
        problems.append("modules missing from the frozen archive: " + ", ".join(missing))

    # sys._MEIPASS in a PyInstaller .app is Contents/Frameworks; runner.py and catalog.py resolve from it.
    meipass = contents / "Frameworks"
    gam7 = meipass / "resources" / "gam7"
    for rel in ("gamgui/web/templates", "gamgui/web/static"):
        for src in sorted((ROOT / rel).rglob("*")):
            if src.is_file() and not (meipass / src.relative_to(ROOT)).is_file():
                problems.append(f"data file missing from the bundle: {src.relative_to(ROOT)}")
    catalog = gam7 / "command_catalog.json"
    if not catalog.is_file():
        problems.append("resources/gam7/command_catalog.json missing (fetch_gam.sh wipes it; restore or regenerate)")
    elif json.loads(catalog.read_text()).get("version") != pin:
        problems.append(f"bundled command_catalog.json is not stamped {pin}")
    if not (gam7 / "GamCommands.txt").is_file():
        problems.append("resources/gam7/GamCommands.txt missing")

    gam = gam7 / "gam"
    if not (gam.is_file() and os.access(gam, os.X_OK)):
        return problems + [f"no executable gam at {gam}"]
    for path in (exe, gam.resolve()):
        if not hardened(path):
            problems.append(f"no hardened runtime on {path.relative_to(app.parent)}")
    home = Path(tempfile.mkdtemp(prefix="gamgui-check-"))
    try:
        cfg = home / "gamcfg"
        cfg.mkdir(mode=0o700)
        env = {"HOME": str(home), "GAMCFGDIR": str(cfg), "PATH": "/usr/bin:/bin"}
        run = subprocess.run([str(gam), "version"], env=env, cwd=home, capture_output=True, text=True, timeout=120)
        if run.returncode or f"GAM {pin}" not in run.stdout:
            problems.append(f"bundled `gam version` (exit {run.returncode}) did not report GAM {pin}:\n"
                            + (run.stdout + run.stderr).strip()[-2000:])
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return problems


def main(argv: list) -> int:
    if len(argv) != 2:
        print("usage: check_app.py <GamGUI.app>", file=sys.stderr)
        return 2
    problems = check(Path(argv[1]).resolve())
    for p in problems:
        print("FAIL:", p, file=sys.stderr)
    if not problems:
        print(f"ok: {argv[1]} — executable, signature, frozen modules, data files, bundled gam")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
