#!/usr/bin/env bash
# Build gamgui/web/static/app.css (committed) from the templates with the pinned Tailwind CLI.
#
# The standalone CLI is fetched once into build/tailwind/, verified against the SHA-256 committed in
# scripts/tailwind_checksums.txt, and re-verified on every run. An asset with no pin is refused.
#
# Usage: scripts/build_css.sh [--check] [--no-fetch]
#   --check     build to a temp file and fail if it differs from the committed app.css (CI)
#   --no-fetch  never download; exit 3 if the pinned CLI is not already cached (the test suite)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# v3, the major the retired in-browser Play CDN ran (3.4.17), so every class renders as it did.
TAILWIND_VERSION="v3.4.17"
OUT="$ROOT/gamgui/web/static/app.css"
CHECK=0
FETCH=1

while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK=1; shift ;;
    --no-fetch) FETCH=0; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

case "$(uname -s)-$(uname -m)" in
  Darwin-arm64) ASSET="tailwindcss-macos-arm64" ;;
  Darwin-x86_64) ASSET="tailwindcss-macos-x64" ;;
  Linux-x86_64) ASSET="tailwindcss-linux-x64" ;;
  Linux-aarch64|Linux-arm64) ASSET="tailwindcss-linux-arm64" ;;
  *) echo "unsupported platform: $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac

PIN="$TAILWIND_VERSION/$ASSET"
EXPECTED="$(awk -v a="$PIN" '$1!~/^#/ && $2==a {print $1}' "$ROOT/scripts/tailwind_checksums.txt" 2>/dev/null | head -1)"
if [ -z "$EXPECTED" ]; then
  echo "ERROR: no pinned checksum for '$PIN' in scripts/tailwind_checksums.txt — refusing to run an unverified binary." >&2
  echo "  Take the hash from the release's sha256sums.txt, check it against a download, and add:" >&2
  echo "    <sha256>  $PIN" >&2
  exit 1
fi

sha() { shasum -a 256 "$1" | awk '{print $1}'; }

CLI="$ROOT/build/tailwind/$PIN"
if [ -f "$CLI" ] && [ "$(sha "$CLI")" != "$EXPECTED" ]; then
  echo "==> Cached $PIN does not match its pin; discarding it." >&2
  rm -f "$CLI"
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if [ ! -f "$CLI" ]; then
  if [ "$FETCH" -eq 0 ]; then
    echo "no cached $PIN (run make css once to fetch it)" >&2
    exit 3
  fi
  echo "==> Downloading Tailwind CLI $PIN..."
  curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 \
    "https://github.com/tailwindlabs/tailwindcss/releases/download/$PIN" -o "$TMP/cli"
  GOT="$(sha "$TMP/cli")"
  if [ "$GOT" != "$EXPECTED" ]; then
    echo "ERROR: checksum mismatch for $PIN" >&2
    echo "  expected (pinned): $EXPECTED" >&2
    echo "  downloaded:        $GOT" >&2
    echo "  Refusing to run it — the binary differs from the committed pin (possible tampering)." >&2
    exit 1
  fi
  mkdir -p "$(dirname "$CLI")"
  mv "$TMP/cli" "$CLI"
fi
chmod +x "$CLI"

# Not --minify: its colour rewriting is lossy (translucent colours come out as rounded hsla(), or
# with other targets an 8-bit alpha), and unminified the CSS renders pixel-identically to what the
# in-browser compiler produced. Readable diffs of the committed file are the other half of the reason.
cd "$ROOT"
BROWSERSLIST_IGNORE_OLD_DATA=1 "$CLI" -c tailwind.config.js -i gamgui/web/tailwind.css -o "$TMP/app.css" \
  2>"$TMP/log" || { cat "$TMP/log" >&2; exit 1; }

if [ "$CHECK" -eq 1 ]; then
  if ! cmp -s "$TMP/app.css" "$OUT"; then
    echo "ERROR: gamgui/web/static/app.css is stale — a template, tailwind.config.js or the pin changed." >&2
    echo "  Run: make css   and commit the result." >&2
    exit 1
  fi
  echo "==> app.css is current."
else
  mv "$TMP/app.css" "$OUT"
  echo "==> Wrote gamgui/web/static/app.css ($(wc -c < "$OUT" | tr -d ' ') bytes)."
fi
