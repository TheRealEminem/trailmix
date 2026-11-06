#!/bin/bash
# Promotes a beta on its own: the newest one that has been out for BETA_DAYS days (default 3) with nobody having an
# open "[Beta X.Y.Z] [blocks me]" issue against it becomes the release everyone gets (like scripts/promote.sh), even if
# a newer beta exists.
# Run daily by .github/workflows/promote-beta.yml.
#   scripts/auto-promote.sh                          -> promotes the newest beta if it's ready, otherwise says why not
#   DRY_RUN=1 scripts/auto-promote.sh                -> only says what it would do (read-only)
#   BETA_DAYS=0 DRY_RUN=1 scripts/auto-promote.sh    -> ignores how long the beta has been out
#   scripts/auto-promote.sh --test-match 0.9.0 "[Beta 0.9.0] [blocks me] Crash"   -> exits 0 if that title blocks 0.9.0
set -euo pipefail
REPO=${TRAILMIX_REPO:-TheRealEminem/trailmix}
BETA_DAYS=${BETA_DAYS:-3}
DRY_RUN=${DRY_RUN:-0}

# Whether an issue title reports a blocker for this exact version: "[Beta 0.9.0] [blocks me] ..." blocks 0.9.0,
# but not 0.9.01 or 10.9.0 (the brackets fence the version), and "[Beta 0.9.0] crash" doesn't block anything.
blocks() {  # blocks VERSION TITLE
  local version=$1 title=$2 found=1
  shopt -s nocasematch
  [[ "$title" == *"[Beta $version]"* && "$title" == *"[blocks me]"* ]] && found=0
  shopt -u nocasematch
  return $found
}

if [[ "${1:-}" == --test-match ]]; then
  blocks "${2:?usage: --test-match X.Y.Z TITLE}" "${3:?usage: --test-match X.Y.Z TITLE}" && { echo "match"; exit 0; }
  echo "no match"; exit 1
fi

# The pre-releases newer than the current release, newest first, and the current release (only plain vX.Y.Z tags;
# drafts ignored). The 3 days count from each beta's own date, so a newer beta never keeps an older one waiting: the
# newest beta that is ready is promoted even when an even newer one is still being tried.
releases=$(gh release list --repo "$REPO" --limit 100 --json tagName,isPrerelease,isDraft,publishedAt)
pick() {  # pick true|false -> "TAG PUBLISHED_AT" lines of the pre-releases / stable releases, newest version first
  jq -r --argjson pre "$1" '
    [.[] | select(.isDraft | not) | select(.isPrerelease == $pre) | select(.tagName | test("^v[0-9]+\\.[0-9]+\\.[0-9]+$"))
     | .v = (.tagName | ltrimstr("v") | split(".") | map(tonumber))]
    | sort_by(.v) | reverse | .[] | "\(.tagName) \(.publishedAt)"' <<<"$releases"
}
STABLE=$(pick false | head -n1 | cut -d' ' -f1)
betas=$(pick true)
[[ -n "$betas" ]] || { echo "Nothing to promote: there's no pre-release."; exit 0; }

# Open "blocks me" reports (listed once)
reports=$(gh issue list --repo "$REPO" --state open --limit 200 --search '"[blocks me]" in:title' \
            --json number,title -q '.[] | "\(.number)\t\(.title)"')  # a failure here stops the script, so it never promotes unchecked

CHOSEN="" AGE="" PLATFORMS=""
while read -r TAG PUBLISHED; do
  VERSION=${TAG#v}
  if [[ -n "$STABLE" ]] && ! jq -en --arg a "$VERSION" --arg b "${STABLE#v}" \
      '($a | split(".") | map(tonumber)) > ($b | split(".") | map(tonumber))' >/dev/null; then
    echo "Not promoting $TAG: it isn't newer than the current release $STABLE."; continue
  fi
  # Days since it was published (jq does the date math, so it's the same on GNU and BSD systems)
  age=$(jq -rn --arg t "$PUBLISHED" '(now - ($t | fromdateiso8601)) / 86400 | . * 10 | floor / 10')
  if ! jq -en --argjson age "$age" --argjson days "$BETA_DAYS" '$age >= $days' >/dev/null; then
    echo "Not promoting $TAG yet: it's been out $age days, under the $BETA_DAYS it needs."; continue
  fi
  blockers=()
  while IFS=$'\t' read -r number title; do
    if [[ -n "$number" ]] && blocks "$VERSION" "$title"; then blockers+=("#$number $title"); fi
  done <<<"$reports"
  if (( ${#blockers[@]} )); then
    echo "Open reports blocking $TAG:"
    printf '  %s\n' "${blockers[@]}"
    echo "Not promoting $TAG: ${#blockers[@]} open \"blocks me\" report(s)."; continue
  fi
  # The computers it has an installer for, as scripts/promote.sh works them out. One whose build failed has none:
  # people on it stay on the last release that has one, while everyone else moves on.
  platforms=$(gh release view "$TAG" --repo "$REPO" --json assets -q '.assets[].name' \
    | sed -nE -e "s/^Trailmix-$VERSION-arm64\.dmg$/macos-arm64/p" -e "s/^Trailmix-$VERSION-([a-z]+-[a-z0-9_]+)\.[a-zA-Z]+$/\1/p" \
    | sort -u | paste -sd ' ' -)
  [[ -n "$platforms" ]] || { echo "Not promoting $TAG: it has no installers."; continue; }
  CHOSEN=$TAG AGE=$age PLATFORMS=$platforms
  break
done <<<"$betas"
[[ -n "$CHOSEN" ]] || { echo "Nothing to promote today."; exit 0; }
TAG=$CHOSEN

if [[ "$DRY_RUN" == 1 ]]; then
  echo "Would run: gh release edit $TAG --repo $REPO --prerelease=false --latest"
  echo "Would promote $TAG (out $AGE days, no open \"blocks me\" reports, installers for: $PLATFORMS; current release ${STABLE:-none}) - dry run, nothing changed."
  exit 0
fi
gh release edit "$TAG" --repo "$REPO" --prerelease=false --latest
echo "Promoted $TAG (out $AGE days, no open \"blocks me\" reports, installers for: $PLATFORMS): it's now the release everyone gets, replacing ${STABLE:-none}."
