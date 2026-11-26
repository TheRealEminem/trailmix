#!/bin/bash
# Tests scripts/publish-release.sh against a stand-in `gh` that keeps the release in a JSON file and writes down
# every call that would change it. Nothing here talks to GitHub.
#   scripts/test-publish-release.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
T=$(mktemp -d "${TMPDIR:-/tmp}/trailmix-publish-test.XXXXXX")
trap 'rm -rf "$T"' EXIT
V=9.8.7 TAG=v9.8.7
FAILED=0

mkdir -p "$T/bin"
cat >"$T/bin/gh" <<'GH'
#!/bin/bash
# release view|create|upload|edit <tag> ... on $GH_STATE/release.json; changes are written to $GH_STATE/calls.
set -euo pipefail
R="$GH_STATE/release.json"
[ "$1" = release ] || { echo "stand-in gh: only 'release' ($*)" >&2; exit 2; }
verb=$2; shift 3
files=() jq_expr=""
while [ $# -gt 0 ]; do
  case "$1" in
    --jq) jq_expr=$2; shift 2 ;;
    --repo|--json|--target|--title) shift 2 ;;
    --*) flags="${flags:-} $1"; shift ;;
    *) files+=("$1"); shift ;;
  esac
done
names() { printf '%s\n' "$@" | jq -R '{name: ., state: "uploaded"}' | jq -s .; }
case "$verb" in
  view)
    [ -f "$R" ] || { echo "release not found" >&2; exit 1; }
    jq -r "$jq_expr" "$R" ;;
  create)
    [ ! -f "$R" ] || { echo "a release with the same tag name already exists" >&2; exit 1; }
    for f in "${files[@]}"; do [ -f "$f" ] || { echo "no such file: $f" >&2; exit 1; }; done
    echo "create ${files[*]}" >>"$GH_STATE/calls"
    jq -n --argjson a "$(names "${files[@]}")" '{isDraft: false, isPrerelease: true, assets: $a}' >"$R" ;;
  upload)
    [ -f "$R" ] || { echo "release not found" >&2; exit 1; }
    for f in "${files[@]}"; do [ -f "$f" ] || { echo "no such file: $f" >&2; exit 1; }; done
    echo "upload ${files[*]}" >>"$GH_STATE/calls"
    jq --argjson a "$(names "${files[@]}")" '.assets = ([.assets[] | select(.name as $n | $a | map(.name) | index($n) | not)] + $a)' "$R" >"$R.new"
    mv "$R.new" "$R" ;;
  edit)
    echo "edit${flags:-}" >>"$GH_STATE/calls"
    jq '.isDraft = false' "$R" >"$R.new"; mv "$R.new" "$R" ;;
  *) echo "stand-in gh: release $verb?" >&2; exit 2 ;;
esac
GH
chmod +x "$T/bin/gh"

# A folder of built files, as the build jobs leave them: built mac-arm mac-intel windows linux (any of them).
built() {
  rm -rf "$T/dist" "$T/state"; mkdir -p "$T/dist" "$T/state"; : >"$T/state/calls"
  local what f
  for what in "$@"; do
    case "$what" in
      mac-arm) set_files="Trailmix-$V-macos-arm64.dmg" ;;
      mac-intel) set_files="Trailmix-$V-macos-x86_64.dmg" ;;
      windows) set_files="Trailmix-$V-windows-x64.exe Trailmix-$V-windows-arm64.exe" ;;
      linux) set_files=$(for a in x86_64 arm64; do for e in AppImage deb rpm; do printf '%s ' "Trailmix-$V-linux-$a.$e"; done; done) ;;
    esac
    for f in $set_files; do echo "$f" >"$T/dist/$f"; echo "sum  $f" >"$T/dist/$f.sha256"; done
  done
  echo x >"$T/dist/Trailmix-$V-macos-x86_64-failed.dmg"; echo x >"$T/dist/window.png"
}
publish() {  # publish [VAR=value ...]: runs the script; its exit code is in $CODE, what it printed in $T/out
  CODE=0
  env "$@" PATH="$T/bin:$PATH" GH_STATE="$T/state" GITHUB_REPOSITORY=someone/trailmix GITHUB_SHA=abc123 \
    "$ROOT/scripts/publish-release.sh" "$TAG" "$T/dist" >"$T/out" 2>&1 || CODE=$?
}
assets() { jq -r '.assets | length' "$T/state/release.json"; }
check() {  # check "what" <command that must succeed>
  local what=$1; shift
  if "$@" >/dev/null 2>&1; then echo "  ok    $what"; else echo "  FAIL  $what"; FAILED=1; sed 's/^/        | /' "$T/out"; sed 's/^/        calls: /' "$T/state/calls"; fi
}
is() { [ "$1" = "$2" ]; }
calls() { wc -l <"$T/state/calls" | tr -d ' '; }
call() { sed -n "$1p" "$T/state/calls"; }

echo "1. A new release, the Intel build having failed"
built mac-arm windows linux; publish
check "it succeeds" is "$CODE" 0
check "one call, which makes the release" is "$(calls) $(call 1 | cut -d' ' -f1)" "1 create"
check "with 30 files" is "$(assets)" 30
check "says Intel Macs have none" grep -q "The Intel build didn't pass" "$T/out"
check "leaves out the failed build and the picture" bash -c "! grep -qE 'failed|png' '$T/state/calls'"
cp "$T/state/release.json" "$T/thirty.json"

echo "2. The Intel build run again: its files are added to the pre-release"
built mac-arm mac-intel windows linux; cp "$T/thirty.json" "$T/state/release.json"; publish
check "it succeeds" is "$CODE" 0
check "the checksum goes up first, alone" is "$(call 1)" "upload Trailmix-$V-macos-x86_64.dmg.sha256"
check "then the disk image and its fixed-name copy" is "$(call 2)" "upload Trailmix-$V-macos-x86_64.dmg Trailmix-macos-x86_64.dmg"
check "and nothing else is touched" is "$(calls) $(assets)" "2 33"

echo "3. Run once more: nothing to add"
publish
check "it succeeds without changing anything" is "$CODE $(calls)" "0 2"
check "and says so" grep -q "nothing to add" "$T/out"

echo "4. The same, after the release was promoted to everyone"
built mac-arm mac-intel windows linux; jq '.isPrerelease = false' "$T/thirty.json" >"$T/state/release.json"; publish
check "it fails" is "$CODE" 1
check "without changing anything" is "$(calls) $(assets)" "0 30"
check "and says why" grep -q "already been promoted to everyone" "$T/out"
publish ALLOW_PROMOTED=1
check "with ALLOW_PROMOTED=1 the files are added" is "$CODE $(calls) $(assets)" "0 2 33"
built mac-arm windows linux; jq '.isPrerelease = false' "$T/thirty.json" >"$T/state/release.json"; publish
check "a promoted release with nothing to add still passes" is "$CODE $(calls)" "0 0"

echo "5. An upload that broke off: a draft with some files, one of them half up"
built mac-arm windows linux
jq '.isDraft = true | .assets = (.assets[:10] | .[9].state = "starter")' "$T/thirty.json" >"$T/state/release.json"
half=$(jq -r '.assets[9].name' "$T/state/release.json"); publish
check "it succeeds" is "$CODE" 0
check "the half-uploaded file goes up again" grep -q -- "$half" "$T/state/calls"
check "the nine that were up don't" bash -c "! grep -qF -- '$(jq -r '.assets[0].name' "$T/thirty.json") ' '$T/state/calls'"
check "then the draft is published" is "$(sed -n '$p' "$T/state/calls")" "edit --draft=false"
check "with all 30 files" is "$(assets) $(jq -r .isDraft "$T/state/release.json")" "30 false"

echo "6. No Apple silicon build: nothing is published"
built mac-intel windows linux; publish
check "it fails without asking GitHub anything" is "$CODE $(calls)" "1 0"
check "and no release was made" test ! -f "$T/state/release.json"

echo "7. The switches keep a platform's files out"
built mac-arm mac-intel windows linux; publish PUBLISH_LINUX=0 PUBLISH_WINDOWS_ARM64=0
check "9 Mac files and 3 Windows x64 ones" is "$CODE $(assets)" "0 12"

echo "8. A built file without its checksum stops the release"
built mac-arm windows; rm "$T/dist/Trailmix-$V-windows-x64.exe.sha256"; publish
check "it fails before anything is made" is "$CODE $(calls)" "1 0"

if [ "$FAILED" = 0 ]; then echo "All checks passed."; else echo "SOME CHECKS FAILED"; exit 1; fi
