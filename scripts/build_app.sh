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

echo "==> Done: dist/GamGUI.app"
echo "    For distribution to other Macs, codesign + notarize (the bundled gam binary must be signed too)."
