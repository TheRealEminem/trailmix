#!/bin/bash
# Makes a tried-out pre-release the release everyone gets: the auto-updater and the download page offer it.
#   scripts/promote.sh v0.7.0
set -euo pipefail
TAG=${1:?usage: scripts/promote.sh vX.Y.Z}
REPO=${TRAILMIX_REPO:-TheRealEminem/trailmix}
gh release view "$TAG" --repo "$REPO" --json isPrerelease -q .isPrerelease | grep -q true \
  || { echo "$TAG isn't a pre-release (already promoted?)"; exit 1; }
gh release edit "$TAG" --repo "$REPO" --prerelease=false --latest
echo "$TAG is now the release everyone gets."
