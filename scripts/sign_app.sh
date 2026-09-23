#!/usr/bin/env bash
# Sign a built GamGUI.app with the hardened runtime. build_app.sh calls this; it also runs alone on a
# built bundle. Usage: scripts/sign_app.sh <GamGUI.app> <identity>   ("-" = ad-hoc)
#
# The hardened runtime makes dyld ignore DYLD_* launch variables and refuses debugger attachment, so a
# same-user `launchctl setenv DYLD_INSERT_LIBRARIES=…` can load code neither into the app the Keychain
# trusts nor into the gam that holds the plaintext credentials.
#
# Entitlements, kept minimal (checked on an arm64 Mac with ad-hoc signatures):
#   app.entitlements  disable-library-validation only. A self-signed or ad-hoc signature has no Team ID,
#                     and library validation refuses a dylib whose Team ID differs from the process's,
#                     i.e. every bundled one. With it on, DYLD_* variables stay ignored; ctypes/pyobjc
#                     callbacks (the system libffi) and WKWebView (its JIT runs in WebKit's own process)
#                     need neither allow-jit nor allow-unsigned-executable-memory.
#   gam.entitlements  upstream GAM's own set, unchanged; tests/test_build_signing.py compares it with
#                     the vendored binary.
set -euo pipefail

APP="$1"
ID="$2"
HERE="$(cd "$(dirname "$0")" && pwd)"
sign() { codesign --force --options runtime --sign "$ID" "$@"; }

# --deep re-signs every nested Mach-O, gam included, with the app's options. So gam is signed after
# it, and the bundle is then resealed (without --deep) so its seal records gam's new signature.
sign --deep --entitlements "$HERE/app.entitlements" "$APP"
GAM_BIN="$(find "$APP" -type f -name gam -path '*resources/gam7/*' | head -1)"
if [ -z "$GAM_BIN" ]; then
  echo "No bundled gam under $APP" >&2
  exit 1
fi
sign --entitlements "$HERE/gam.entitlements" "$GAM_BIN"
sign --entitlements "$HERE/app.entitlements" "$APP"
codesign --verify --deep --strict "$APP"
