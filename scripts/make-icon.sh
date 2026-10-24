#!/usr/bin/env bash
# Renders the full-bleed logo (scripts/AppIcon.svg) into a macOS .icns file: scripts/make-icon.sh <out.icns>
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:?usage: make-icon.sh <out.icns>}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# The rendered icon is kept in scripts/AppIcon.icns (committed), because Quick Look needs a desktop session and
# CI has none. Delete that file, or change the logo, to render it again.
CACHED="$ROOT/scripts/AppIcon.icns"
if [ -f "$CACHED" ] && [ "$CACHED" -nt "$ROOT/scripts/AppIcon.svg" ]; then
  mkdir -p "$(dirname "$OUT")"
  cp "$CACHED" "$OUT"
  exit 0
fi

# Quick Look renders the SVG. The artwork fills the whole 1024px square, with no rounded corners or margin: newer
# macOS masks icons itself, and one that arrives pre-masked and padded is shown small on a gray plate.
if qlmanage -t -s 1024 -o "$TMP" "$ROOT/scripts/AppIcon.svg" >/dev/null 2>&1 && [ -f "$TMP/AppIcon.svg.png" ]; then
  SET="$TMP/AppIcon.iconset"
  mkdir -p "$SET"
  for s in 16 32 128 256 512; do
    sips -z $s $s "$TMP/AppIcon.svg.png" --out "$SET/icon_${s}x${s}.png" >/dev/null
    sips -z $((s * 2)) $((s * 2)) "$TMP/AppIcon.svg.png" --out "$SET/icon_${s}x${s}@2x.png" >/dev/null
  done
  mkdir -p "$(dirname "$OUT")"
  iconutil -c icns "$SET" -o "$OUT"
  cp "$OUT" "$CACHED" 2>/dev/null || true
else
  echo "(Couldn't render the icon; the app will use the default one.)" >&2
  exit 1
fi
