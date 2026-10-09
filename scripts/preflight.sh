#!/bin/bash
# Everything a release must pass, run on this Mac before tagging, so a broken build fails here in a few
# minutes instead of on GitHub. Then tag and push: GitHub builds the release and also tests updating from the
# current one (that part isn't run here: a second full Trailmix would take over your recording shortcut).
#   scripts/preflight.sh            # checks the version in VERSION
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION=$(cat "$ROOT/VERSION")
OUT=$(mktemp -d /tmp/trailmix-preflight.XXXXXX)
trap 'rm -rf "$OUT"' EXIT
step() { printf '\n\033[1;32m›\033[0m %s\n' "$*"; }

step "Backend tests"
(cd "$ROOT/backend" && .venv/bin/python -m pytest -q tests)

step "Web app: types and build"
(cd "$ROOT/frontend" && npx tsc --noEmit -p . && npm run build --silent >/dev/null)

step "Building Trailmix $VERSION"
TRAILMIX_OUT="$OUT" TRAILMIX_VERSION="$VERSION" "$ROOT/scripts/build-app.sh" --dmg >"$OUT/build.log" 2>&1 \
  || { tail -30 "$OUT/build.log"; exit 1; }

step "Smoke test: a fresh install"
"$ROOT/scripts/smoke-test.sh" "$OUT/Trailmix.app"

step "Ready: tag v$VERSION and push"
