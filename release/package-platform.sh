#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=release/lib.sh
source "$ROOT/release/lib.sh"

validate_archive() {
  local version="$1" archive="$2"
  [ -f "$archive" ] || release_fail "platform archive is missing: $archive"
  local expected
  expected="$(platform_archive "$version")"
  [ "$(basename "$archive")" = "$expected" ] \
    || release_fail "unexpected platform archive name: $(basename "$archive")"

  local listing
  listing="$(mktemp)"
  tar -tzf "$archive" > "$listing"
  awk '
    /^\// { exit 1 }
    { path=$0; sub(/^\.\//, "", path); if (path ~ /(^|\/)\.\.($|\/)/) exit 1 }
  ' "$listing" || release_fail "platform archive contains an unsafe path"
  grep -Fx './docs/kuasar-sandbox.md' "$listing" >/dev/null \
    || release_fail "platform archive is missing docs/kuasar-sandbox.md"
  grep -Fx './test/e2e/run_all.sh' "$listing" >/dev/null \
    || release_fail "platform archive is missing test/e2e/run_all.sh"
  if grep -E '(^|/)release\.json$|(^|/)release/[^/]+\.json$' "$listing" >/dev/null; then
    release_fail "platform archive contains release metadata JSON"
  fi
  if awk '
    { path=$0; sub(/^\.\//, "", path) }
    path != "" && path !~ /\/$/ && path !~ /^(docs|test)\// { exit 1 }
  ' "$listing"; then
    :
  else
    release_fail "platform archive contains files outside docs/ and test/"
  fi
  rm -f "$listing"
}

package_archive() {
  [ "$#" -eq 2 ] || release_fail "usage: package-platform.sh package <release-version> <output-dir>"
  local version="$1" output="$2"
  validate_aggregate_version "$version"
  assert_safe_output "$output"
  local epoch="${SOURCE_DATE_EPOCH:-0}"
  [[ "$epoch" =~ ^[0-9]+$ ]] || release_fail "SOURCE_DATE_EPOCH must be an integer"

  local work stage archive
  work="$(mktemp -d)"
  stage="$work/stage"
  mkdir -p "$stage" "$output/assets"
  cp -a "$ROOT/docs" "$ROOT/test" "$stage/"
  archive="$(platform_archive "$version")"
  tar --sort=name --owner=0 --group=0 --numeric-owner --mtime="@$epoch" \
    --pax-option=delete=atime,delete=ctime -czf "$output/assets/$archive" -C "$stage" .
  validate_archive "$version" "$output/assets/$archive"
  cat > "$output/release-notes.md" <<EOF
Kuasar Sandbox platform integration package for $version.

The package contains the system documentation and the deliverable cross-component E2E, performance, and demo suites. Component binaries are published as separate assets in the same aggregate release.
EOF
  rm -rf "$work"
}

case "${1:-}" in
  package)
    shift
    package_archive "$@"
    ;;
  validate)
    shift
    [ "$#" -eq 2 ] || release_fail "usage: package-platform.sh validate <release-version> <archive>"
    validate_aggregate_version "$1"
    validate_archive "$1" "$2"
    ;;
  *)
    release_fail "usage: package-platform.sh <package <release-version> <output-dir>|validate <release-version> <archive>>"
    ;;
esac
