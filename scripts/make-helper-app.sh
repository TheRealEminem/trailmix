#!/usr/bin/env bash
# Builds "Trailmix Helper.app": the menu bar recorder that captures your mic plus other apps' audio
# (Zoom, Teams, FaceTime…), with global hotkeys. Needs macOS 14.2+ and the Xcode command line tools.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG="$ROOT/helper"
APP="$ROOT/Trailmix Helper.app"
PORT="${TRAILMIX_PORT:-8765}"

command -v swift >/dev/null || { echo "Building the helper needs the Xcode command line tools: xcode-select --install" >&2; exit 1; }
swift build -c release --package-path "$PKG"
BIN="$(swift build -c release --package-path "$PKG" --show-bin-path)/TrailmixHelper"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/TrailmixHelper"

# Same icon as Trailmix.app.
"$ROOT/scripts/make-icon.sh" "$APP/Contents/Resources/AppIcon.icns" || true

cat >"$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Trailmix Helper</string>
  <key>CFBundleDisplayName</key><string>Trailmix Helper</string>
  <key>CFBundleIdentifier</key><string>local.trailmix.helper</string>
  <key>CFBundleExecutable</key><string>TrailmixHelper</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>14.2</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Trailmix Helper records your side of the conversation.</string>
  <key>NSAudioCaptureUsageDescription</key><string>Trailmix Helper records the other side of your calls from apps like Zoom, Teams and FaceTime.</string>
  <key>TrailmixServerURL</key><string>http://127.0.0.1:$PORT</string>
  <key>TrailmixRoot</key><string>$ROOT</string>
</dict></plist>
PLIST

# Ad-hoc signature: enough for macOS to remember the microphone and system-audio permissions. After a
# rebuild the signature changes, so macOS asks for those permissions once more.
codesign --force --sign - "$APP" >/dev/null 2>&1
touch "$APP"
echo "Built $APP"
