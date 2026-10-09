#!/bin/bash
# Promotes the newest beta on its own: once a pre-release has been out for BETA_DAYS days (default 3) and nobody
# has an open "[Beta X.Y.Z] [blocks me]" issue against it, it becomes the release everyone gets (like scripts/promote.sh).
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

# Newest beta and newest stable release, by version (only plain vX.Y.Z tags; drafts ignored)
releases=$(gh release list --repo "$REPO" --limit 100 --json tagName,isPrerelease,isDraft,publishedAt)
pick() {  # pick true|false -> "TAG PUBLISHED_AT" of the newest pre-release / stable release, or nothing
  jq -r --argjson pre "$1" '
    [.[] | select(.isDraft | not) | select(.isPrerelease == $pre) | select(.tagName | test("^v[0-9]+\\.[0-9]+\\.[0-9]+$"))
     | .v = (.tagName | ltrimstr("v") | split(".") | map(tonumber))]
    | max_by(.v) // empty | "\(.tagName) \(.publishedAt)"' <<<"$releases"
}
read -r TAG PUBLISHED <<<"$(pick true)" || true
read -r STABLE _ <<<"$(pick false)" || true

[[ -n "${TAG:-}" ]] || { echo "Nothing to promote: there's no pre-release."; exit 0; }
VERSION=${TAG#v}

if [[ -n "${STABLE:-}" ]] && ! jq -en --arg a "$VERSION" --arg b "${STABLE#v}" \
    '($a | split(".") | map(tonumber)) > ($b | split(".") | map(tonumber))' >/dev/null; then
  echo "Nothing to promote: the newest pre-release $TAG isn't newer than the current release $STABLE."; exit 0
fi

# Days since it was published (jq does the date math, so it's the same on GNU and BSD systems)
AGE=$(jq -rn --arg t "$PUBLISHED" '(now - ($t | fromdateiso8601)) / 86400 | . * 10 | floor / 10')
if ! jq -en --argjson age "$AGE" --argjson days "$BETA_DAYS" '$age >= $days' >/dev/null; then
  echo "Not promoting $TAG yet: it's been out $AGE days, under the $BETA_DAYS it needs."; exit 0
fi

# Open "blocks me" reports for this version
reports=$(gh issue list --repo "$REPO" --state open --limit 200 --search '"[blocks me]" in:title' \
            --json number,title -q '.[] | "\(.number)\t\(.title)"')  # a failure here stops the script, so it never promotes unchecked
blockers=()
while IFS=$'\t' read -r number title; do
  if [[ -n "$number" ]] && blocks "$VERSION" "$title"; then blockers+=("#$number $title"); fi
done <<<"$reports"
if (( ${#blockers[@]} )); then
  echo "Open reports blocking $TAG:"
  printf '  %s\n' "${blockers[@]}"
  echo "Not promoting $TAG: ${#blockers[@]} open \"blocks me\" report(s)."; exit 0
fi

if [[ "$DRY_RUN" == 1 ]]; then
  echo "Would run: gh release edit $TAG --repo $REPO --prerelease=false --latest"
  echo "Would promote $TAG (out $AGE days, no open \"blocks me\" reports; current release ${STABLE:-none}) - dry run, nothing changed."
  exit 0
fi
gh release edit "$TAG" --repo "$REPO" --prerelease=false --latest
echo "Promoted $TAG (out $AGE days, no open \"blocks me\" reports): it's now the release everyone gets, replacing ${STABLE:-none}."
