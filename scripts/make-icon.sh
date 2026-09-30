#!/usr/bin/env bash
# Renders the logo (frontend/public/favicon.svg) into a macOS .icns file: scripts/make-icon.sh <out.icns>
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:?usage: make-icon.sh <out.icns>}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# The rendered icon is kept in scripts/AppIcon.icns (committed), because Quick Look needs a desktop session and
# CI has none. Delete that file, or change the logo, to render it again.
CACHED="$ROOT/scripts/AppIcon.icns"
if [ -f "$CACHED" ] && [ "$CACHED" -nt "$ROOT/frontend/public/favicon.svg" ]; then
  mkdir -p "$(dirname "$OUT")"
  cp "$CACHED" "$OUT"
  exit 0
fi

# Quick Look renders the SVG; macOS icons sit inside a margin (824px artwork on a 1024px canvas).
if qlmanage -t -s 1024 -o "$TMP" "$ROOT/frontend/public/favicon.svg" >/dev/null 2>&1 && [ -f "$TMP/favicon.svg.png" ]; then
  sips -z 824 824 "$TMP/favicon.svg.png" --out "$TMP/art.png" >/dev/null
  sips -p 1024 1024 "$TMP/art.png" --out "$TMP/favicon.svg.png" >/dev/null
  SET="$TMP/AppIcon.iconset"
  mkdir -p "$SET"
  for s in 16 32 128 256 512; do
    sips -z $s $s "$TMP/favicon.svg.png" --out "$SET/icon_${s}x${s}.png" >/dev/null
    sips -z $((s * 2)) $((s * 2)) "$TMP/favicon.svg.png" --out "$SET/icon_${s}x${s}@2x.png" >/dev/null
  done
  mkdir -p "$(dirname "$OUT")"
  iconutil -c icns "$SET" -o "$OUT"
  cp "$OUT" "$CACHED" 2>/dev/null || true
else
  echo "(Couldn't render the icon; the app will use the default one.)" >&2
  exit 1
fi
