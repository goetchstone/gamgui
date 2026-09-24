#!/usr/bin/env bash
# Build the standalone GamGUI.app (macOS) with PyInstaller.
# Prereqs: a Python 3.10+ to freeze — `make setup`'s .venv, or PYTHON=/path/to/python3.x. Vendors
# GAM7 automatically if missing.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-.venv/bin/python}"
if ! command -v "$PY" >/dev/null; then
  echo "No virtualenv at .venv — run 'make setup' first, or set PYTHON." >&2
  exit 1
fi

if [ "$(uname)" != "Darwin" ]; then
  echo "Note: this builds a macOS .app; on $(uname) PyInstaller will produce a plain bundle instead." >&2
fi

if [ ! -x "gamgui/resources/gam7/gam" ]; then
  echo "==> GAM binary not vendored; fetching..."
  ./scripts/fetch_gam.sh
fi

echo "==> Installing the locked dependencies into a fresh build venv..."
# Everything the bundle holds — pywebview (the WKWebView host), PyInstaller's bootloader, the web
# stack — comes from requirements/app.txt, each file checked against its committed SHA-256. A venv
# of its own, so nothing the dev venv happens to have ends up in the .app. The lock doubles as the
# build constraint: pywebview's proxy-tools ships only as source, and this hash-checks the setuptools
# that builds it. --build-constraint needs pip >= 25.3 (the one bundled with Python 3.14 has it).
BUILD_VENV="build/venv"
rm -rf "$BUILD_VENV"
"$PY" -m venv "$BUILD_VENV"
"$BUILD_VENV/bin/python" -m pip install -q --require-hashes \
  --build-constraint requirements/app.txt -r requirements/app.txt

echo "==> Building..."
"$BUILD_VENV/bin/python" -m PyInstaller --noconfirm --clean gamgui.spec

APP="dist/GamGUI.app"
# Sign with a STABLE self-signed identity so macOS "Always Allow" sticks across rebuilds and the
# Keychain stops re-prompting. Create a free "Code Signing" cert (Keychain Access, or the CLI in the
# README) named "GamGUI Local" once; builds then pick it up automatically. Override with
# CODESIGN_IDENTITY=… ; with no such cert the bundle is signed ad-hoc. Either way it gets the
# hardened runtime (scripts/sign_app.sh).
if [ "$(uname)" = "Darwin" ]; then
  if [ -z "${CODESIGN_IDENTITY:-}" ] \
     && security find-identity -p codesigning 2>/dev/null | grep -q "GamGUI Local"; then
    CODESIGN_IDENTITY="GamGUI Local"  # auto-use the local signing cert if it exists
  fi
  if [ -n "${CODESIGN_IDENTITY:-}" ]; then
    echo "==> Codesigning (hardened runtime) with stable identity: $CODESIGN_IDENTITY"
  else
    echo "==> No CODESIGN_IDENTITY set — signing ad-hoc (hardened runtime)."
    echo "    For a silent Keychain, make a self-signed Code Signing cert and re-run with"
    echo "    CODESIGN_IDENTITY set (see README → 'Stop the Keychain prompts')."
  fi
  ./scripts/sign_app.sh "$APP" "${CODESIGN_IDENTITY:--}"
  echo "    signed + verified OK"
fi

echo "==> Done: $APP"
echo "    For distribution to OTHER Macs you still need an Apple Developer ID + notarization."
