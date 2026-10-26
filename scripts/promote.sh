#!/bin/bash
# Makes a tried-out pre-release the release everyone gets: the auto-updater and the download page offer it.
#   scripts/promote.sh v0.7.0
set -euo pipefail
TAG=${1:?usage: scripts/promote.sh vX.Y.Z}
REPO=${TRAILMIX_REPO:-TheRealEminem/trailmix}
gh release view "$TAG" --repo "$REPO" --json isPrerelease -q .isPrerelease | grep -q true \
  || { echo "$TAG isn't a pre-release (already promoted?)"; exit 1; }
# The computers it has an installer for, from the names (Trailmix-<version>-<os>-<arch>.<ext>; Trailmix-<version>-arm64.dmg
# is the Apple silicon one's name from before there were others). One whose build failed has none: people on it stay
# on the last release that has one.
V=${TAG#v}
PLATFORMS=$(gh release view "$TAG" --repo "$REPO" --json assets -q '.assets[].name' \
  | sed -nE -e "s/^Trailmix-$V-arm64\.dmg$/macos-arm64/p" -e "s/^Trailmix-$V-([a-z]+-[a-z0-9_]+)\.[a-zA-Z]+$/\1/p" \
  | sort -u | paste -sd ' ' -)
[ -n "$PLATFORMS" ] || { echo "$TAG has no installers, so there's nothing to promote"; exit 1; }
gh release edit "$TAG" --repo "$REPO" --prerelease=false --latest
echo "$TAG is now the release everyone gets (installers for: $PLATFORMS)."
