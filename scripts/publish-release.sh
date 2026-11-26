#!/bin/bash
# What release.yml's publish job runs: makes the GitHub release for a tag from the files the build jobs made. Run
# again for a release that's already there (a build that failed was run again, or the upload broke off), it adds
# the files the release doesn't have yet and never replaces one that's already up.
#   scripts/publish-release.sh v0.24.0 [folder]     (the folder holds the built files; default: dist)
# Needs gh (GH_TOKEN), GITHUB_REPOSITORY and GITHUB_SHA. PUBLISH_WINDOWS, PUBLISH_WINDOWS_ARM64 and PUBLISH_LINUX
# ("1" or "0", default "1") are release.yml's switches.
#
# Each disk image is named for the computer it's for, Trailmix-<version>-macos-<arch>.dmg (the app's updater picks
# its own by that name), and has a copy with a fixed name, Trailmix-macos-<arch>.dmg, so a link to
# releases/latest/download/... always works ("latest" skips pre-releases: the last promoted version).
# Also under the names from before there were other platforms (Trailmix-<version>-arm64.dmg, Trailmix-arm64.dmg):
# apps from then look for those, and links to them are out there. Each Windows installer,
# Trailmix-<version>-windows-<arch>.exe (+ .sha256, + the fixed name Trailmix-windows-<arch>.exe), and each Linux
# package comes along when its build passed (and its switch is on).
#
# Added to a release that's already there:
# - a pre-release: the files it lacks, checksums first, so an updater never sees a file before its checksum;
# - a draft (gh makes one, uploads, then publishes: a draft is an upload that broke off): the same, then published;
# - a release that's been promoted to everyone: nothing, and the run fails saying so. Adding a build there gives it
#   to everyone with no days as a beta, which is for a person to decide (ALLOW_PROMOTED=1, which release.yml sets
#   when the run was started with "To everyone", or `gh release upload` by hand).
set -euo pipefail
TAG=${1:?usage: publish-release.sh vX.Y.Z [folder with the built files]}
cd "${2:-dist}"
REPO=${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is not set}
SHA=${GITHUB_SHA:?GITHUB_SHA is not set}
V="${TAG#v}"

rm -f ./*-failed* ./*.png  # (never published)
[ -f "Trailmix-$V-macos-arm64.dmg" ] || { echo "::error::The Apple silicon build didn't pass, so nothing is published"; exit 1; }
[ -f "Trailmix-$V-macos-x86_64.dmg" ] || echo "::warning::The Intel build didn't pass: this release has none, and Intel Macs stay on the last one that did"

# The release's files: checksums apart, so they can go up first.
SUMS=() REST=()
add() {
  local f
  for f in "$@"; do
    [ -f "$f" ] || { echo "::error::$f is missing from the built files"; exit 1; }
    case "$f" in *.sha256) SUMS+=("$f") ;; *) REST+=("$f") ;; esac
  done
}

for dmg in Trailmix-"$V"-macos-*.dmg; do
  cp "$dmg" "Trailmix-${dmg#Trailmix-"$V"-}"
  add "$dmg" "$dmg.sha256" "Trailmix-${dmg#Trailmix-"$V"-}"
done
cp "Trailmix-$V-macos-arm64.dmg" "Trailmix-$V-arm64.dmg"
shasum -a 256 "Trailmix-$V-arm64.dmg" >"Trailmix-$V-arm64.dmg.sha256"
cp Trailmix-macos-arm64.dmg Trailmix-arm64.dmg
add "Trailmix-$V-arm64.dmg" "Trailmix-$V-arm64.dmg.sha256" Trailmix-arm64.dmg

for arch in x64 arm64; do
  if [ "${PUBLISH_WINDOWS:-1}" != 1 ] || { [ "$arch" = arm64 ] && [ "${PUBLISH_WINDOWS_ARM64:-1}" != 1 ]; }; then
    echo "Windows $arch installer built and tested, not published (PUBLISH_WINDOWS, PUBLISH_WINDOWS_ARM64)"
  elif [ -f "Trailmix-$V-windows-$arch.exe" ]; then
    cp "Trailmix-$V-windows-$arch.exe" "Trailmix-windows-$arch.exe"
    add "Trailmix-$V-windows-$arch.exe" "Trailmix-$V-windows-$arch.exe.sha256" "Trailmix-windows-$arch.exe"
  else
    echo "::warning::The Windows $arch build didn't pass: this release has no installer for those PCs"
  fi
done

for arch in x86_64 arm64; do
  if [ "${PUBLISH_LINUX:-1}" != 1 ]; then
    echo "Linux $arch packages built and tested, not published (PUBLISH_LINUX)"
  elif [ -f "Trailmix-$V-linux-$arch.AppImage" ]; then
    for ext in AppImage deb rpm; do
      [ -f "Trailmix-$V-linux-$arch.$ext" ] || { echo "::warning::No Linux $arch .$ext this time (its test didn't pass)"; continue; }
      cp "Trailmix-$V-linux-$arch.$ext" "Trailmix-linux-$arch.$ext"
      add "Trailmix-$V-linux-$arch.$ext" "Trailmix-$V-linux-$arch.$ext.sha256" "Trailmix-linux-$arch.$ext"
    done
  else
    echo "::warning::The Linux $arch build didn't pass: this release has nothing for those computers"
  fi
done

# Is the release there already? Two lines of true/false (a draft, a pre-release), then the files that are fully up.
# (If GitHub can't be asked, this takes it for not there, and making it below fails if it is.)
if state=$(gh release view "$TAG" --repo "$REPO" --json isDraft,isPrerelease,assets \
    --jq '(.isDraft | tostring), (.isPrerelease | tostring), (.assets[] | select(.state == "uploaded") | .name)' 2>/dev/null); then
  draft=$(sed -n 1p <<<"$state")
  prerelease=$(sed -n 2p <<<"$state")
  have=$(tail -n +3 <<<"$state")
  NEW_SUMS=() NEW_REST=()
  for f in "${SUMS[@]}"; do grep -qxF -- "$f" <<<"$have" || NEW_SUMS+=("$f"); done
  for f in "${REST[@]}"; do grep -qxF -- "$f" <<<"$have" || NEW_REST+=("$f"); done
  new=$((${#NEW_SUMS[@]} + ${#NEW_REST[@]}))

  if [ "$new" -eq 0 ] && [ "$draft" != true ]; then
    echo "::notice::$TAG already has every file this run built: nothing to add"
    exit 0
  fi
  if [ "$draft" != true ] && [ "$prerelease" != true ] && [ "${ALLOW_PROMOTED:-0}" != 1 ]; then
    echo "::error::$TAG has already been promoted to everyone, so these $new files were not added (they'd reach everyone with no days as a beta): ${NEW_REST[*]-} ${NEW_SUMS[*]-}"
    echo "They're in this run's artifacts. To add them anyway, download them and run: gh release upload $TAG <the .sha256 files first> --repo $REPO"
    exit 1
  fi
  # --clobber only clears a file of the same name that didn't finish uploading: the ones fully up aren't in these lists.
  [ ${#NEW_SUMS[@]} -eq 0 ] || gh release upload "$TAG" "${NEW_SUMS[@]}" --repo "$REPO" --clobber
  [ ${#NEW_REST[@]} -eq 0 ] || gh release upload "$TAG" "${NEW_REST[@]}" --repo "$REPO" --clobber
  if [ "$draft" = true ]; then
    gh release edit "$TAG" --repo "$REPO" --draft=false
    echo "::notice::$TAG was a draft (its upload had broken off): added $new files and published it"
  else
    echo "::notice::Added $new files to $TAG, which was already published: ${NEW_REST[*]-}"
  fi
  exit 0
fi

# --target makes the tag on this commit when the run was started with "Release it" (a tag made with this
# workflow's token doesn't start another run, so it isn't built twice).
gh release create "$TAG" "${SUMS[@]}" "${REST[@]}" \
  --repo "$REPO" --target "$SHA" --title "Trailmix $V" --generate-notes --prerelease
