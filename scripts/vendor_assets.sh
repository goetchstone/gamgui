#!/usr/bin/env bash
# Vendor the front-end assets into gamgui/web/static/vendor/ so the UI loads NO remote scripts:
# it works offline and a CDN compromise can't inject JS that could read the launch token.
# Re-run when bumping htmx/Tailwind. htmx is verified against its published SRI; the Tailwind Play
# CDN is versionless, so we snapshot it and record its sha256 for auditability.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/gamgui/web/static/vendor"
mkdir -p "$DEST"

HTMX_VER="1.9.12"
HTMX_SRI="sha384-ujb1lZYygJmzgSwoxRggbCHcjc0rB2XoQrxeTUQyRjrOnlCoYta87iKBWq3EsdM2"
echo "==> htmx ${HTMX_VER}"
curl -fsSL "https://unpkg.com/htmx.org@${HTMX_VER}/dist/htmx.min.js" -o "$DEST/htmx-${HTMX_VER}.min.js"
GOT="sha384-$(openssl dgst -sha384 -binary "$DEST/htmx-${HTMX_VER}.min.js" | openssl base64 -A)"
if [ "$GOT" != "$HTMX_SRI" ]; then
  echo "ERROR: htmx SRI mismatch — refusing to vendor a tampered file." >&2
  echo "  expected: $HTMX_SRI" >&2
  echo "  got:      $GOT" >&2
  exit 1
fi
echo "    verified SRI $GOT"

echo "==> Tailwind Play (versionless JIT compiler — snapshot)"
curl -fsSL "https://cdn.tailwindcss.com" -o "$DEST/tailwind-play.js"
echo "    sha256 $(shasum -a 256 "$DEST/tailwind-play.js" | awk '{print $1}')"

echo "==> Done. If filenames changed, update the <script> tags in gamgui/web/templates/base.html."
