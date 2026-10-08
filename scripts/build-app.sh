#!/usr/bin/env bash
# Builds dist/Trailmix.app: one self-contained Mac app. It holds the menu bar recorder, the Trailmix
# server (with its own Python, MLX and ffmpeg) and the web app, so nothing else needs installing.
#
#   scripts/build-app.sh          build dist/Trailmix.app
#   scripts/build-app.sh --dmg    also make dist/Trailmix-<version>-arm64.dmg, the file you hand out
#
# Needs: Apple silicon, macOS 14.2+, the Xcode command line tools (swift), Node.js and internet access
# (downloads the Python runtime once into build/cache, plus Python packages).
#
# Signing: ad-hoc by default, which is fine on your own Mac and for people who accept the "unidentified
# developer" prompt. To sign for real, set TRAILMIX_SIGN_IDENTITY="Developer ID Application: …" (this turns
# on the hardened runtime too; notarizing the result is a separate `xcrun notarytool` step).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${TRAILMIX_VERSION:-$(tr -d '[:space:]' <"$ROOT/VERSION")}"
PORT="${TRAILMIX_PORT:-8765}"
# Where the app looks for updates (GitHub owner/repo): TRAILMIX_UPDATE_REPO, else this checkout's origin.
UPDATE_REPO="${TRAILMIX_UPDATE_REPO:-$(git -C "$ROOT" remote get-url origin 2>/dev/null | sed -E 's#(\.git)?$##; s#^.*github\.com[:/]##')}"
OUT="${TRAILMIX_OUT:-$ROOT/dist}"  # where the app (and disk image) go
APP="$OUT/Trailmix.app"
RES="$APP/Contents/Resources"
CACHE="$ROOT/build/cache"

# python-build-standalone (https://github.com/astral-sh/python-build-standalone): a relocatable CPython.
PY_VERSION="3.14.7"
PY_RELEASE="20260929"
PY_FILE="cpython-$PY_VERSION+$PY_RELEASE-aarch64-apple-darwin-install_only_stripped.tar.gz"
PY_SHA256="44b4716f4e63bc85e1c07ea2aca730d13a0ec3557d06526d84a13b1d111cf4f9"

say() { printf '\033[1;32m›\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] || die "Trailmix.app is built on an Apple silicon Mac (MLX needs Apple silicon)."
command -v swift >/dev/null || die "Building needs the Xcode command line tools: xcode-select --install"
command -v npm >/dev/null || die "Building needs Node.js (brew install node)."

# ── Web app ────────────────────────────────────────────────────────────
say "Building the web app…"
(cd "$ROOT/frontend" && { [ -d node_modules ] || npm ci --silent; } && npm run -s build >/dev/null)

# ── Menu bar app ───────────────────────────────────────────────────────
say "Building the menu bar app…"
swift build -c release --package-path "$ROOT/helper" 2>&1 | grep -E "error|warning: unre|Compiling|Build complete" | tail -n 3 || true
BIN="$(swift build -c release --package-path "$ROOT/helper" --show-bin-path)/TrailmixHelper"
[ -x "$BIN" ] || die "The menu bar app didn't build. Run: swift build -c release --package-path helper"

# ── Bundle ─────────────────────────────────────────────────────────────
say "Assembling Trailmix.app…"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$RES/bin"
cp "$BIN" "$APP/Contents/MacOS/Trailmix"
"$ROOT/scripts/make-icon.sh" "$RES/AppIcon.icns" || true

cat >"$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Trailmix</string>
  <key>CFBundleDisplayName</key><string>Trailmix</string>
  <key>CFBundleIdentifier</key><string>local.trailmix.app</string>
  <key>CFBundleExecutable</key><string>Trailmix</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>14.2</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.productivity</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Trailmix records your side of the conversation.</string>
  <key>NSAudioCaptureUsageDescription</key><string>Trailmix records the other side of your calls from apps like Zoom, Teams and FaceTime.</string>
  <key>TrailmixServerURL</key><string>http://127.0.0.1:$PORT</string>
  <key>TrailmixUpdateRepo</key><string>$UPDATE_REPO</string>
</dict></plist>
PLIST

# ── Python runtime ─────────────────────────────────────────────────────
mkdir -p "$CACHE"
if [ ! -f "$CACHE/$PY_FILE" ]; then
  say "Downloading the Python runtime ($PY_VERSION, once)…"
  curl -fL --progress-bar -o "$CACHE/$PY_FILE.part" "https://github.com/astral-sh/python-build-standalone/releases/download/$PY_RELEASE/${PY_FILE//+/%2B}"
  mv "$CACHE/$PY_FILE.part" "$CACHE/$PY_FILE"
fi
[ "$(shasum -a 256 "$CACHE/$PY_FILE" | cut -d' ' -f1)" = "$PY_SHA256" ] || { rm -f "$CACHE/$PY_FILE"; die "The downloaded Python runtime failed its checksum; try again."; }
tar -xzf "$CACHE/$PY_FILE" -C "$RES"   # -> Resources/python
PY="$RES/python/bin/python3"

say "Installing Python packages…"
export PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1
"$PY" -m pip install -q --no-compile -r "$ROOT/backend/requirements-app.txt"
"$PY" -m pip install -q --no-compile --no-deps "mlx-whisper>=0.4"

# ffmpeg: a static build from the imageio-ffmpeg wheel (only system frameworks, includes the Opus encoder).
FF_TMP="$(mktemp -d)"
trap 'rm -rf "$FF_TMP"' EXIT
"$PY" -m pip install -q --no-compile --no-deps --target "$FF_TMP" imageio-ffmpeg
cp "$FF_TMP"/imageio_ffmpeg/binaries/ffmpeg-macos-aarch64-* "$RES/bin/ffmpeg"
chmod +x "$RES/bin/ffmpeg"

# ── Slim down ──────────────────────────────────────────────────────────
say "Trimming…"
LIB="$RES/python/lib/python${PY_VERSION%.*}"
SITE="$LIB/site-packages"
rm -rf "$SITE"/pip "$SITE"/pip-* "$SITE"/setuptools "$SITE"/setuptools-* "$SITE"/pkg_resources "$SITE"/_distutils_hack "$SITE"/distutils-precedence.pth
rm -rf "$LIB"/test "$LIB"/idlelib "$LIB"/tkinter "$LIB"/turtledemo "$LIB"/ensurepip "$LIB"/config-* "$LIB"/lib-dynload/_tkinter*
rm -rf "$RES"/python/include "$RES"/python/share "$RES"/python/lib/pkgconfig "$RES"/python/lib/*.a "$SITE"/mlx/include
rm -rf "$RES"/python/lib/tcl* "$RES"/python/lib/tk* "$RES"/python/lib/itcl* "$RES"/python/lib/thread* "$RES"/python/lib/libtcl* "$RES"/python/lib/libtk*
# Only the interpreter stays in bin/: the launchers pip made point at the build machine, and leftovers would be dangling links.
find "$RES/python/bin" -mindepth 1 ! -name python3 ! -name python3.14 -delete
find "$SITE" -type d \( -name tests -o -name test \) -prune -exec rm -rf {} +
find "$RES/python" -name __pycache__ -type d -prune -exec rm -rf {} +

# ── Trailmix itself ────────────────────────────────────────────────────
mkdir -p "$RES/backend"
rsync -a --include='*.py' --include='assets/' --include='assets/**' --exclude='*' "$ROOT/backend/" "$RES/backend/"
rsync -a --delete "$ROOT/frontend/dist/" "$RES/ui/"
cp "$ROOT/VERSION" "$RES/VERSION"

# Compile ahead of time, never re-checked against the source: the app is never written to while it runs
# (that would break its signature), and starts faster.
"$PY" -m compileall -q -j 0 --invalidation-mode unchecked-hash "$RES/python/lib" "$RES/backend" >/dev/null

# ── Sign ───────────────────────────────────────────────────────────────
IDENTITY="${TRAILMIX_SIGN_IDENTITY:--}"
SIGN=(codesign --force --sign "$IDENTITY")
if [ "$IDENTITY" != "-" ]; then SIGN+=(--timestamp --options runtime); fi
say "Signing ($([ "$IDENTITY" = "-" ] && echo "ad-hoc" || echo "$IDENTITY"))…"
# Every Mach-O file inside first, then the app around them.
while IFS= read -r -d '' f; do
  case "$(file -b "$f")" in *Mach-O*) "${SIGN[@]}" "$f" >/dev/null 2>&1 || die "Couldn't sign $f" ;; esac
done < <(find "$APP" -type f -print0)
ENTITLEMENTS=()
[ "$IDENTITY" != "-" ] && ENTITLEMENTS=(--entitlements "$ROOT/scripts/Trailmix.entitlements")
"${SIGN[@]}" ${ENTITLEMENTS[@]+"${ENTITLEMENTS[@]}"} "$APP" >/dev/null 2>&1 || die "Couldn't sign the app"
codesign --verify --strict "$APP" 2>&1 | head -n 3

du -sh "$APP" | awk -v app="$APP" '{print "› Built " app " (" $1 ")"}'

# ── Disk image ─────────────────────────────────────────────────────────
if [ "${1:-}" = "--dmg" ]; then
  DMG="$OUT/Trailmix-$VERSION-arm64.dmg"
  STAGE="$(mktemp -d)"
  trap 'rm -rf "$FF_TMP" "$STAGE"' EXIT
  say "Making the disk image…"
  ditto "$APP" "$STAGE/Trailmix.app"
  ln -s /Applications "$STAGE/Applications"
  cp "$ROOT/scripts/Open Anyway.txt" "$STAGE/Read me first.txt" 2>/dev/null || true
  # The window people see when they open the disk image: a background with an arrow from Trailmix to
  # Applications and the "Open Anyway" reminder (scripts/dmg-background.svg, rendered to the .png at 2x).
  mkdir -p "$STAGE/.background"
  sips -s dpiWidth 144 -s dpiHeight 144 "$ROOT/scripts/dmg-background.png" --out "$STAGE/.background/background.png" >/dev/null
  rm -f "$DMG"
  # hdiutil fails now and then on build machines ("Resource busy"): try each step a few times, and say so.
  retry() {
    for attempt in 1 2 3 4; do
      "$@" && return 0
      echo "  ($1 ${2:-} failed, attempt $attempt; trying again)" >&2
      sleep $((attempt * 5))
    done
    echo "  $* failed" >&2
    return 1
  }
  RW="$STAGE.rw.dmg"
  MNT="$STAGE.mnt"
  retry hdiutil create -quiet -volname "Trailmix" -srcfolder "$STAGE" -format UDRW -fs HFS+ -ov "$RW"
  retry hdiutil attach -quiet -readwrite -noverify -noautoopen -mountpoint "$MNT" "$RW"
  # Finder lays out the window (needs a desktop session; skipped without one, leaving a plain window).
  if ! /usr/bin/osascript >/dev/null 2>&1 <<APPLESCRIPT
tell application "Finder"
  set f to (POSIX file "$MNT" as alias)
  open f
  set w to container window of f
  set current view of w to icon view
  set toolbar visible of w to false
  set statusbar visible of w to false
  set bounds of w to {200, 120, 860, 568}
  set opts to icon view options of w
  set arrangement of opts to not arranged
  set icon size of opts to 100
  set text size of opts to 13
  set background picture of opts to file ".background:background.png" of f
  set position of item "Trailmix.app" of f to {165, 165}
  set position of item "Applications" of f to {495, 165}
  set position of item "Read me first.txt" of f to {600, 285}
  update f without registering applications
  delay 1
  close w
end tell
APPLESCRIPT
  then
    echo "  (Couldn't lay out the disk image window; it will look plain.)"
  fi
  chflags hidden "$MNT/.background" "$MNT/.fseventsd" 2>/dev/null || true  # for people who show hidden files
  sync
  hdiutil detach -quiet "$MNT" || retry hdiutil detach -quiet -force "$MNT"
  retry hdiutil convert -quiet "$RW" -format ULFO -o "$DMG" -ov
  rm -f "$RW"
  # The app's updater checks a download against this before installing it.
  (cd "$OUT" && shasum -a 256 "$(basename "$DMG")" >"$(basename "$DMG").sha256")
  du -sh "$DMG" | awk -v dmg="$DMG" '{print "› Built " dmg " (" $1 ")"}'
fi
