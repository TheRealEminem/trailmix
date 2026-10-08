#!/bin/bash
# The release gate: tries a built Trailmix.app the way you use it, with its own bundled engine, and fails
# loudly if anything a user would hit is broken. Nothing touches your real Trailmix data.
#
#   scripts/smoke-test.sh dist/Trailmix.app                                      # the new version on its own
#   scripts/smoke-test.sh dist/Trailmix.app dist/Trailmix-0.7.0-arm64.dmg old.dmg  # ...after updating from old.dmg
#
# With a previous version it first installs that one, opens its window (so its page is cached, as on your
# Mac), lets its auto-updater install the new disk image from a stand-in for GitHub, and checks the update
# landed. The rest then runs on the updated app and the old version's data:
#   the menu bar recorder's checks (TrailmixHelper --self-test: hotkeys, marks, live draft, stop from the
#   window), then Record clicked in the real window right after a restart, a transcript made while
#   recording, and the window running the current web app rather than a cached one.
# Speech comes from `say`; transcription uses the small whisper-tiny model so it's quick.
set -euo pipefail

APP=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
NEW_DMG=${2:-}
OLD_DMG=${3:-}
PORT=${TRAILMIX_SMOKE_PORT:-8797}
FEED_PORT=$((PORT + 1))
URL="http://127.0.0.1:$PORT"
T=$(mktemp -d /tmp/trailmix-smoke.XXXXXX)
HOME_DIR="$T/home"
DATA="$T/data"
mkdir -p "$HOME_DIR" "$DATA" "$T/Applications" "$T/feed"
HF_CACHE=${HF_HOME:-$HOME/.cache/huggingface}  # keep sharing downloaded models between runs
PIDS=()
ENGINE=""
cleanup() {
  for pid in "${PIDS[@]:-}"; do [ -n "$pid" ] && kill "$pid" 2>/dev/null || true; done
  pkill -f "uvicorn main:app --host 127.0.0.1 --port $PORT" 2>/dev/null || true
  for _ in $(seq 1 20); do pgrep -f "uvicorn main:app --host 127.0.0.1 --port $PORT" >/dev/null || break; sleep 0.5; done
  hdiutil detach -force "$T/old-mount" >/dev/null 2>&1 || true
}
trap cleanup EXIT
log() { printf '\n== %s\n' "$*"; }
fail() { echo "SMOKE TEST FAILED: $*" >&2; echo "--- server log (last 60 lines)" >&2; tail -60 "$T/server.log" >&2 || true; exit 1; }
# Something already answering on the port would be tested instead of this app.
if curl -s -o /dev/null "$URL/api/health" 2>/dev/null; then fail "something is already running at $URL"; fi

# The environment the app gets: hermetic, small models, no browser windows.
ENV=(HOME="$HOME_DIR" PATH=/usr/bin:/bin:/usr/sbin:/sbin HF_HOME="$HF_CACHE"
     TRAILMIX_DATA_DIR="$DATA" TRAILMIX_SERVER_URL="$URL" TRAILMIX_NO_OPEN=1
     TRAILMIX_WHISPER_MODEL=mlx-community/whisper-tiny-mlx TRAILMIX_LIVE_MODEL=mlx-community/whisper-tiny-mlx
     TRAILMIX_WHISPER_RAM_GB=0.5)

version_of() { /usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$1/Contents/Info.plist"; }

wait_for_server() {
  for _ in $(seq 1 120); do
    curl -sf "$URL/api/health" >/dev/null 2>&1 && return 0
    sleep 1
  done
  fail "the engine didn't start at $URL"
}

# The engine exactly as the app runs it (ServerProcess.swift), without the app around it.
start_engine() {
  local res="$1/Contents/Resources"
  (cd "$res/backend" && exec env -i "${ENV[@]}" PATH="$res/bin:/usr/bin:/bin" PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \
     PYTHONUNBUFFERED=1 TRAILMIX_UI_DIR="$res/ui" \
     "$res/python/bin/python3" -m uvicorn main:app --host 127.0.0.1 --port "$PORT" >>"$T/server.log" 2>&1) &
  ENGINE=$!
  PIDS+=("$ENGINE")
  wait_for_server
}

stop_engine() {
  kill "$ENGINE" 2>/dev/null || true
  pkill -f "uvicorn main:app --host 127.0.0.1 --port $PORT" 2>/dev/null || true
  for _ in $(seq 1 30); do curl -s -o /dev/null "$URL/api/health" 2>/dev/null || return 0; sleep 0.5; done
  fail "the engine at $URL didn't stop"
}

log "Speech for the test meeting"
say -o "$T/me.wav" --file-format=WAVE --data-format=LEI16@16000 \
  "Good morning everyone. Let's go over the launch plan. The beta goes out on Tuesday and we need the login fix before then."
say -o "$T/them.wav" --file-format=WAVE --data-format=LEI16@16000 \
  "Sounds good. I will fix the login bug by Wednesday and send the pricing page to marketing tomorrow."

UNDER_TEST="$APP"
if [ -n "$OLD_DMG" ]; then
  [ -n "$NEW_DMG" ] || fail "give the new disk image too, to update to it"
  NEW_VERSION=$(version_of "$APP")
  log "Installing the previous version from $(basename "$OLD_DMG")"
  hdiutil attach -nobrowse -readonly -noverify -mountpoint "$T/old-mount" "$OLD_DMG" >/dev/null
  ditto "$T/old-mount/Trailmix.app" "$T/Applications/Trailmix.app"
  hdiutil detach -force "$T/old-mount" >/dev/null
  OLD_VERSION=$(version_of "$T/Applications/Trailmix.app")
  echo "previous version: $OLD_VERSION, new version: $NEW_VERSION"

  # A stand-in for GitHub's "latest release", offering the new disk image.
  cp "$NEW_DMG" "$T/feed/Trailmix-$NEW_VERSION-arm64.dmg"
  (cd "$T/feed" && shasum -a 256 "Trailmix-$NEW_VERSION-arm64.dmg" >"Trailmix-$NEW_VERSION-arm64.dmg.sha256")
  cat >"$T/feed/latest.json" <<EOF
{"tag_name": "v$NEW_VERSION", "prerelease": false, "draft": false, "html_url": "http://127.0.0.1:$FEED_PORT/",
 "assets": [
  {"name": "Trailmix-$NEW_VERSION-arm64.dmg", "browser_download_url": "http://127.0.0.1:$FEED_PORT/Trailmix-$NEW_VERSION-arm64.dmg"},
  {"name": "Trailmix-$NEW_VERSION-arm64.dmg.sha256", "browser_download_url": "http://127.0.0.1:$FEED_PORT/Trailmix-$NEW_VERSION-arm64.dmg.sha256"}]}
EOF
  echo "[$(cat "$T/feed/latest.json")]" >"$T/feed/releases.json"
  (cd "$T/feed" && exec python3 -m http.server "$FEED_PORT" --bind 127.0.0.1 >/dev/null 2>&1) &
  PIDS+=($!)

  log "Running the previous version, with its window opened once"
  env -i "${ENV[@]}" TRAILMIX_UPDATE_URL="http://127.0.0.1:$FEED_PORT/latest.json" TRAILMIX_UPDATE_DELAY=2 \
    TRAILMIX_UPDATE_NO_RELAUNCH=1 "$T/Applications/Trailmix.app/Contents/MacOS/Trailmix" >>"$T/old-app.log" 2>&1 &
  OLD_APP=$!
  PIDS+=("$OLD_APP")
  wait_for_server
  env -i "${ENV[@]}" "$T/Applications/Trailmix.app/Contents/MacOS/Trailmix" --window-test "$URL" "$T/old-window.png" 4 \
    >/dev/null 2>&1 || echo "(the previous version couldn't snapshot its window; carrying on)"

  log "Updating it the way you would: Update now"
  for _ in $(seq 1 60); do
    curl -s "$URL/api/recorder" | grep -q '"state":"available"' && break
    sleep 1
  done
  curl -s "$URL/api/recorder" | grep -q '"state":"available"' || fail "the previous version never offered the update"
  curl -sf -X POST "$URL/api/recorder/update" >/dev/null || fail "couldn't ask the previous version to update"
  for _ in $(seq 1 180); do kill -0 "$OLD_APP" 2>/dev/null || break; sleep 1; done
  kill -0 "$OLD_APP" 2>/dev/null && fail "the previous version didn't finish updating (still running after 3 minutes)"
  GOT=$(version_of "$T/Applications/Trailmix.app")
  [ "$GOT" = "$NEW_VERSION" ] || fail "after updating, Trailmix.app is $GOT, not $NEW_VERSION"
  codesign --verify --strict "$T/Applications/Trailmix.app" || fail "the updated app's signature is broken"
  echo "updated $OLD_VERSION -> $GOT"
  stop_engine
  UNDER_TEST="$T/Applications/Trailmix.app"
fi

log "Starting $(version_of "$UNDER_TEST")'s engine"
start_engine "$UNDER_TEST"
SERVED=$(curl -s "$URL/" | grep -o 'assets/index-[A-Za-z0-9_-]*\.js' | head -1)
[ -f "$UNDER_TEST/Contents/Resources/ui/$SERVED" ] || fail "the engine at $URL isn't this app's (it serves $SERVED)"

log "Speech models (downloaded on first start, as during setup)"
for _ in $(seq 1 300); do
  curl -s "$URL/api/models" | grep -q '"installed":false' || break
  sleep 1
done
curl -s "$URL/api/models" | grep -q '"installed":false' && fail "the speech models didn't download"
echo "ready"

log "The recorder and the window, end to end"
env -i "${ENV[@]}" "$UNDER_TEST/Contents/MacOS/Trailmix" --self-test "$URL" "$T/me.wav" "$T/them.wav" \
  || fail "the app's own checks failed (above)"

echo
echo "SMOKE TEST PASSED ($(version_of "$UNDER_TEST")${OLD_DMG:+, updated from $OLD_VERSION})"
