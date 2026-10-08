#!/bin/bash
# Saves the disk image window's layout (background, icon positions, window size) as scripts/dmg-DS_Store,
# which build-app.sh copies into each disk image. Run it again after changing the background or positions.
# It needs Finder, so it runs on a Mac with a desktop, not on the build machine.
#   scripts/make-dmg-layout.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
T=$(mktemp -d)
STAGE="$T/stage"
MNT="/Volumes/Trailmix"  # where people will see it (Finder lays out windows of mounted volumes there)
[ -e "$MNT" ] && { echo "Eject the Trailmix disk image first"; exit 1; }
mkdir -p "$STAGE/Trailmix.app" "$STAGE/.background"
ln -s /Applications "$STAGE/Applications"
cp "$ROOT/scripts/Open Anyway.txt" "$STAGE/Read me first.txt"
sips -s dpiWidth 144 -s dpiHeight 144 "$ROOT/scripts/dmg-background.png" --out "$STAGE/.background/background.png" >/dev/null
hdiutil create -quiet -volname "Trailmix" -srcfolder "$STAGE" -format UDRW -fs HFS+ -size 20m -ov "$T/layout.dmg"
hdiutil attach -quiet -readwrite -noverify -noautoopen -mountpoint "$MNT" "$T/layout.dmg"
/usr/bin/osascript <<APPLESCRIPT
tell application "Finder"
  set f to (POSIX file "$MNT" as alias)
  open f
  delay 2
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
  delay 2
  close w
end tell
APPLESCRIPT
sync
sleep 2
cp "$MNT/.DS_Store" "$ROOT/scripts/dmg-DS_Store"
hdiutil detach -quiet "$MNT" || hdiutil detach -quiet -force "$MNT"
rm -rf "$T"
echo "Saved scripts/dmg-DS_Store"
