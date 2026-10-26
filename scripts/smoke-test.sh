#!/bin/bash
# The release gate: tries a built Trailmix.app the way you use it, with its own bundled engine, and fails
# loudly if anything a user would hit is broken. Nothing touches your real Trailmix data.
#
#   scripts/smoke-test.sh dist/Trailmix.app                                            # the new version on its own
#   scripts/smoke-test.sh dist/Trailmix.app dist/Trailmix-0.7.0-macos-arm64.dmg old.dmg  # ...after updating from old.dmg
#
# With a previous version it first installs that one, opens its window (so its page is cached, as on your
# Mac), lets its auto-updater install the new disk image from a stand-in for GitHub (naming it as release.yml
# does), and checks the update landed. The rest then runs on the updated app and the old version's data:
#   the menu bar recorder's checks (TrailmixHelper --self-test: hotkeys, marks, live draft, stop from the
#   window), then Record clicked in the real window right after a restart, a transcript made while
#   recording, and the window running the current web app rather than a cached one. Last, it goes back to the
#   previous version (Settings → Updates → Go back) and checks that version opens the meetings the new one left,
#   while a newer release with no disk image for this Mac is passed over rather than offered.
# Speech comes from scripts/fixtures; transcription uses the small whisper-tiny model so it's quick. The checks that
# are the same on every computer (is it this app's engine, are its models in, does a meeting get transcribed) are in
# scripts/smoke_test.py, which the other platforms' smoke tests use too.
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
SCRIPTS=$(cd "$(dirname "$0")" && pwd)
ME_WAV="$SCRIPTS/fixtures/me.wav"
THEM_WAV="$SCRIPTS/fixtures/them.wav"
# The small model for the app's own engine: MLX on Apple silicon, whisper.cpp on an Intel Mac.
if ls -d "$APP"/Contents/Resources/python/lib/python3*/site-packages/mlx_whisper >/dev/null 2>&1; then
  TINY=mlx-community/whisper-tiny-mlx
else
  TINY=ggerganov/whisper.cpp:ggml-tiny.bin
fi
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
     TRAILMIX_WHISPER_MODEL="$TINY" TRAILMIX_LIVE_MODEL="$TINY"
     TRAILMIX_WHISPER_RAM_GB=0.5
     # Not under test: on a Mac with under 10% of its disk free, the first recording's notes would wait for
     # "memory" and hold every recording queued behind them, so no transcript would arrive.
     TRAILMIX_SWAP_DISK_FREE=0
     # No notes AI, as on GitHub's runners: this Mac's own Ollama could park the notes waiting for memory (with a
     # nearly full disk) and hold up every recording queued behind them.
     OLLAMA_URL=http://127.0.0.1:9)

version_of() { /usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$1/Contents/Info.plist"; }

# The stand-in for GitHub's releases. publish DMG NAME... puts the disk image in it under each name, with its checksum;
# release_json VERSION FILE... is GitHub's JSON for a release of those files.
publish() {
  local dmg=$1 name
  shift
  for name in "$@"; do
    cp "$dmg" "$T/feed/$name"
    (cd "$T/feed" && shasum -a 256 "$name" >"$name.sha256")
  done
}
release_json() {
  local version=$1 assets="" file
  shift
  for file in "$@"; do
    assets+="${assets:+, }{\"name\": \"$file\", \"browser_download_url\": \"http://127.0.0.1:$FEED_PORT/$file\"}"
  done
  echo "{\"tag_name\": \"v$version\", \"prerelease\": false, \"draft\": false, \"html_url\": \"http://127.0.0.1:$FEED_PORT/\", \"assets\": [$assets]}"
}
with_checksums() { local name; for name in "$@"; do echo "$name" "$name.sha256"; done; }

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

UNDER_TEST="$APP"
if [ -n "$OLD_DMG" ]; then
  [ -n "$NEW_DMG" ] || fail "give the new disk image too, to update to it"
  NEW_VERSION=$(version_of "$APP")
  # The kind of Mac it's for, from the disk image's name (Trailmix-<version>-macos-<arch>.dmg)
  ARCH=$(basename "$NEW_DMG" | sed -nE 's/.*-macos-([a-z0-9_]+)\.dmg$/\1/p')
  ARCH=${ARCH:-$(uname -m)}
  log "Installing the previous version from $(basename "$OLD_DMG")"
  hdiutil attach -nobrowse -readonly -noverify -mountpoint "$T/old-mount" "$OLD_DMG" >/dev/null
  ditto "$T/old-mount/Trailmix.app" "$T/Applications/Trailmix.app"
  hdiutil detach -force "$T/old-mount" >/dev/null
  OLD_VERSION=$(version_of "$T/Applications/Trailmix.app")
  echo "previous version: $OLD_VERSION, new version: $NEW_VERSION"

  # A stand-in for GitHub's "latest release", offering the new disk image under the names release.yml gives it
  # (on Apple silicon also the name from before there were other platforms, which older apps look for).
  NEW_NAMES=("Trailmix-$NEW_VERSION-macos-$ARCH.dmg")
  [ "$ARCH" = arm64 ] && NEW_NAMES+=("Trailmix-$NEW_VERSION-arm64.dmg")
  publish "$NEW_DMG" "${NEW_NAMES[@]}"
  release_json "$NEW_VERSION" $(with_checksums "${NEW_NAMES[@]}") >"$T/feed/latest.json"
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
  for i in $(seq 1 90); do
    curl -s "$URL/api/recorder" | grep -q '"state":"available"' && break
    # As Help → Check for Updates… does (in case its first look came before everything was up).
    [ $((i % 10)) = 5 ] && curl -s -o /dev/null -X POST "$URL/api/recorder/check-update"
    sleep 1
  done
  if ! curl -s "$URL/api/recorder" | grep -q '"state":"available"'; then
    echo "The previous version reports: $(curl -s "$URL/api/recorder")" >&2
    echo "Feed: $(curl -s "http://127.0.0.1:$FEED_PORT/latest.json" | head -c 300)" >&2
    echo "--- its log" >&2; tail -30 "$T/old-app.log" >&2
    fail "the previous version never offered the update"
  fi
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

NEW_VERSION_LABEL=$(version_of "$UNDER_TEST")
log "Starting $NEW_VERSION_LABEL's engine"
start_engine "$UNDER_TEST"
log "Speech models (downloaded on first start, as during setup)"
python3 "$SCRIPTS/smoke_test.py" check-engine "$URL" "$UNDER_TEST/Contents/Resources/ui" || fail "(above)"

log "The recorder and the window, end to end"
env -i "${ENV[@]}" "$UNDER_TEST/Contents/MacOS/Trailmix" --self-test "$URL" "$ME_WAV" "$THEM_WAV" \
  || fail "the app's own checks failed (above)"

log "A recording imported from a file"
python3 "$SCRIPTS/smoke_test.py" transcribe "$URL" "$THEM_WAV" login Wednesday pricing || fail "(above)"

count_meetings() { python3 "$SCRIPTS/smoke_test.py" meetings "$URL"; }

if [ -n "$OLD_DMG" ]; then
  log "Going back to $OLD_VERSION the way you would: Settings → Updates → Go back"
  MEETINGS_BEFORE=$(count_meetings) || fail "couldn't count the meetings"
  stop_engine
  # The stand-in for GitHub now lists both versions, as GitHub does: on Apple silicon the previous one under the
  # name releases had before there were other platforms (the updater falls back to it), plus a newer release built
  # only for other computers, which the updater has to pass over rather than offer.
  OLD_NAME="Trailmix-$OLD_VERSION-macos-$ARCH.dmg"
  [ "$ARCH" = arm64 ] && OLD_NAME="Trailmix-$OLD_VERSION-arm64.dmg"
  publish "$OLD_DMG" "$OLD_NAME"
  echo "[$(release_json 999.0.0 Trailmix-999.0.0-windows-x86_64.exe Trailmix-999.0.0-linux-x86_64.AppImage),
        $(cat "$T/feed/latest.json"),
        $(release_json "$OLD_VERSION" $(with_checksums "$OLD_NAME"))]" >"$T/feed/releases.json"
  env -i "${ENV[@]}" TRAILMIX_UPDATE_URL="http://127.0.0.1:$FEED_PORT/releases.json" \
    TRAILMIX_RELEASES_URL="http://127.0.0.1:$FEED_PORT/releases.json" TRAILMIX_UPDATE_DELAY=2 \
    TRAILMIX_UPDATE_NO_RELAUNCH=1 "$UNDER_TEST/Contents/MacOS/Trailmix" >>"$T/new-app.log" 2>&1 &
  NEW_APP=$!
  PIDS+=("$NEW_APP")
  wait_for_server
  for i in $(seq 1 60); do
    REPORT=$(curl -s "$URL/api/recorder")
    grep -q "\"previous\":\"$OLD_VERSION\"" <<<"$REPORT" && grep -Eq '"state":"(up-to-date|available)"' <<<"$REPORT" && break
    [ $((i % 10)) = 5 ] && curl -s -o /dev/null -X POST "$URL/api/recorder/check-update"
    sleep 1
  done
  if ! grep -q "\"previous\":\"$OLD_VERSION\"" <<<"$REPORT"; then
    echo "It reports: $REPORT" >&2
    echo "--- its log" >&2; tail -20 "$T/new-app.log" >&2
    fail "it never offered to go back to $OLD_VERSION"
  fi
  grep -q '"state":"available"' <<<"$REPORT" && fail "it offered an update with no disk image for this Mac: $REPORT"
  curl -sf -X POST "$URL/api/recorder/go-back" >/dev/null || fail "couldn't ask it to go back"
  for _ in $(seq 1 180); do kill -0 "$NEW_APP" 2>/dev/null || break; sleep 1; done
  kill -0 "$NEW_APP" 2>/dev/null && fail "going back didn't finish (still running after 3 minutes)"
  GOT=$(version_of "$UNDER_TEST")
  [ "$GOT" = "$OLD_VERSION" ] || fail "after going back, Trailmix.app is $GOT, not $OLD_VERSION"
  codesign --verify --strict "$UNDER_TEST" || fail "the version it went back to has a broken signature"
  stop_engine

  log "$OLD_VERSION opens the meetings $NEW_VERSION left"
  start_engine "$UNDER_TEST"
  MEETINGS_AFTER=$(count_meetings) || fail "$OLD_VERSION couldn't list the meetings"
  [ "$MEETINGS_AFTER" = "$MEETINGS_BEFORE" ] || fail "$OLD_VERSION sees $MEETINGS_AFTER meetings; $NEW_VERSION had $MEETINGS_BEFORE"
  echo "went back $NEW_VERSION -> $GOT, with all $MEETINGS_AFTER meetings"
  stop_engine
fi

echo
echo "SMOKE TEST PASSED ($NEW_VERSION_LABEL${OLD_DMG:+, updated from $OLD_VERSION and back again})"
