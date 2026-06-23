#!/usr/bin/env bash
# Build the standalone GamGUI.app (macOS) with PyInstaller.
# Prereqs: `make setup` (a .venv with deps). Vendors GAM7 automatically if missing.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-.venv/bin/python}"
if [ ! -x "$PY" ]; then
  echo "No virtualenv at .venv — run 'make setup' first." >&2
  exit 1
fi

if [ "$(uname)" != "Darwin" ]; then
  echo "Note: this builds a macOS .app; on $(uname) PyInstaller will produce a plain bundle instead." >&2
fi

if [ ! -x "gamgui/resources/gam7/gam" ]; then
  echo "==> GAM binary not vendored; fetching..."
  ./scripts/fetch_gam.sh
fi

echo "==> Installing PyInstaller (and the native window) into the venv..."
"$PY" -m pip install -q --upgrade pyinstaller "pywebview>=5.1"

echo "==> Building..."
"$PY" -m PyInstaller --noconfirm --clean gamgui.spec

APP="dist/GamGUI.app"
# Sign with a STABLE self-signed identity so macOS "Always Allow" sticks across rebuilds and the
# Keychain stops re-prompting. Create a free "Code Signing" cert (Keychain Access, or the CLI in the
# README) named "GamGUI Local" once; builds then pick it up automatically. Override with
# CODESIGN_IDENTITY=… ; leave it with no such cert to keep PyInstaller's ad-hoc signature.
if [ -z "${CODESIGN_IDENTITY:-}" ] && [ "$(uname)" = "Darwin" ] \
   && security find-identity -p codesigning 2>/dev/null | grep -q "GamGUI Local"; then
  CODESIGN_IDENTITY="GamGUI Local"  # auto-use the local signing cert if it exists
fi
if [ -n "${CODESIGN_IDENTITY:-}" ] && [ "$(uname)" = "Darwin" ]; then
  echo "==> Codesigning with stable identity: $CODESIGN_IDENTITY"
  GAM_BIN="$(find "$APP" -type f -name gam -path '*resources/gam7/*' 2>/dev/null | head -1)"
  [ -n "$GAM_BIN" ] && codesign --force --sign "$CODESIGN_IDENTITY" "$GAM_BIN"
  codesign --force --deep --sign "$CODESIGN_IDENTITY" "$APP"
  codesign --verify --deep --strict "$APP" && echo "    signed + verified OK"
else
  echo "==> No CODESIGN_IDENTITY set — keeping the ad-hoc signature."
  echo "    For a silent Keychain, make a self-signed Code Signing cert and re-run with"
  echo "    CODESIGN_IDENTITY set (see README → 'Stop the Keychain prompts')."
fi

echo "==> Done: $APP"
echo "    For distribution to OTHER Macs you still need an Apple Developer ID + notarization."
