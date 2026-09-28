#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLATFORM_SOURCE_ROOT="$(cd "${PLATFORM_SOURCE_ROOT:-$ROOT}" && pwd)"
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
  grep -Fx './guide/kuasar-sandbox.md' "$listing" >/dev/null \
    || release_fail "platform archive is missing guide/kuasar-sandbox.md"
  grep -Fx './test/e2e/e2e' "$listing" >/dev/null \
    || release_fail "platform archive is missing test/e2e/e2e"
  grep -Fx './test/e2e/lib/common.sh' "$listing" >/dev/null \
    || release_fail "platform archive is missing test/e2e/lib/common.sh"
  grep -Fx './test/e2e/lib/workspace.py' "$listing" >/dev/null \
    || release_fail "platform archive is missing prepared workspace helpers"
  if grep -E '^\./test/e2e/([^/]+/)?run_all\.sh$|generated-compatibility-runners' "$listing" >/dev/null; then
    release_fail "platform archive contains a superseded E2E runner"
  fi
  python3 -B "$ROOT/release/validate-e2e-package.py" "$archive"
  local owner
  for owner in accelerator connector guest-runtime sandboxer orchestrator; do
    grep -Fx "./guide/$owner/README.md" "$listing" >/dev/null \
      || release_fail "platform archive is missing guide/$owner/README.md"
  done
  grep -Fx './workbench/workbench' "$listing" >/dev/null \
    || release_fail 'platform archive is missing the workbench launcher'
  if grep -E '/test_[^/]*$|/test/perf/|/assemble(_docs)?\.(sh|py)$|/package_inputs\.py$' "$listing" >/dev/null; then
    release_fail 'platform archive contains source-only tests or assembly tools'
  fi
  if grep -Fx './test/e2e/assemble.sh' "$listing" >/dev/null; then
    release_fail "platform archive contains the source-only E2E assembler"
  fi
  if grep -E '(^|/)release\.json$|(^|/)release/[^/]+\.json$' "$listing" >/dev/null; then
    release_fail "platform archive contains release metadata JSON"
  fi
  if awk '
    { path=$0; sub(/^\.\//, "", path) }
    path != "" && path !~ /\/$/ && path !~ /^(guide|test|workbench)\// { exit 1 }
  ' "$listing"; then
    :
  else
    release_fail "platform archive contains files outside guide/, test/ and workbench/"
  fi
  rm -f "$listing"
}

package_archive() {
  [ "$#" -eq 4 ] \
    || release_fail "usage: package-platform.sh package <release-version> <component-source-dir> <test-source-dir> <output-dir>"
  local version="$1" sources="$2" test_sources="$3" output="$4"
  validate_aggregate_version "$version"
  [ -n "$test_sources" ] && [ -d "$test_sources" ] \
    || release_fail "independently pinned test source directory is missing"
  local unit
  for unit in accelerator connector runtime vmlinux sandboxer orchestrator; do
    [ -d "$sources/$unit" ] || release_fail "component source is missing: $sources/$unit"
  done
  assert_safe_output "$output"
  local epoch="${SOURCE_DATE_EPOCH:-0}"
  [[ "$epoch" =~ ^[0-9]+$ ]] || release_fail "SOURCE_DATE_EPOCH must be an integer"

  local work stage archive
  work="$(mktemp -d)"
  stage="$work/stage"
  mkdir -p "$output/assets"
  [ -f "$sources/vmlinux/docs/vmlinux.md" ] \
    || release_fail "vmlinux source is missing docs/vmlinux.md"
  resolve_selection "$PLATFORM_SOURCE_ROOT" "$version" "$work/docs-refs.tsv"
  python3 "$ROOT/release/selection.py" "$PLATFORM_SOURCE_ROOT" "$version" --test-revisions \
    > "$work/test-revisions.json"
  python3 - "$work/docs-refs.tsv" "$work/test-revisions.json" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
pins = json.loads(pathlib.Path(sys.argv[2]).read_text())
refs = dict(line.split('\t') for line in path.read_text().splitlines())
for unit in refs:
    if unit != 'vmlinux':
        refs[unit] = pins['guest-runtime' if unit == 'runtime' else unit]
path.write_text(''.join(f'{unit}\t{ref}\n' for unit, ref in refs.items()))
PY
  printf 'platform\t%s\n' "$version" >> "$work/docs-refs.tsv"
  E2E_SOURCE_ROOT="$test_sources" DOCS_SOURCE_REFS="$work/docs-refs.tsv" DOCS_VMLINUX_SOURCE="$sources/vmlinux" \
    E2E_HELPER_ROOT="${E2E_HELPER_ROOT:?provide the prebuilt E2E helper packages}" \
    "$ROOT/test/e2e/assemble.sh" "$stage" "$PLATFORM_SOURCE_ROOT" \
    "$test_sources/accelerator" "$test_sources/connector" "$test_sources/guest-runtime" \
    "$test_sources/sandboxer" "$test_sources/orchestrator"
  archive="$(platform_archive "$version")"
  tar --sort=name --owner=0 --group=0 --numeric-owner --mtime="@$epoch" \
    --pax-option=delete=atime,delete=ctime -czf "$output/assets/$archive" -C "$stage" .
  validate_archive "$version" "$output/assets/$archive"
  cat > "$output/release-notes.md" <<EOF
Kuasar Sandbox platform integration package for $version.

The package contains the selected bilingual user guides, canonical E2E cases and runtime inputs, locked Demo dependencies, and the thin workbench launcher. Component binaries are published as separate assets in the same aggregate release.
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
    release_fail "usage: package-platform.sh <package <release-version> <component-source-dir> <test-source-dir> <output-dir>|validate <release-version> <archive>>"
    ;;
esac
