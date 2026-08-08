#!/usr/bin/env bash

set -euo pipefail

readonly RELEASE_ORGANIZATION=kuasar-sandbox
readonly RELEASE_TARGET=x86_64
readonly RELEASE_UNITS=(accelerator connector sandboxer orchestrator runtime vmlinux)

release_fail() {
  echo "release: $*" >&2
  exit 1
}

validate_component_version() {
  [[ "$1" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-preview\.[0-9]{8})?$ ]] \
    || release_fail "component version must match vX.Y.Z or vX.Y.Z-preview.YYYYMMDD"
}

validate_aggregate_version() {
  [[ "$1" =~ ^release-v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-preview\.[0-9]{8})?$ ]] \
    || release_fail "aggregate version must match release-vX.Y.Z or release-vX.Y.Z-preview.YYYYMMDD"
}

validate_unit_version() {
  local unit="$1" version="$2"
  case "$unit" in
    accelerator|connector|sandboxer|orchestrator)
      validate_component_version "$version"
      ;;
    runtime)
      [[ "$version" =~ ^runtime-v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-preview\.[0-9]{8})?$ ]] \
        || release_fail "runtime version must match runtime-vX.Y.Z or runtime-vX.Y.Z-preview.YYYYMMDD"
      ;;
    vmlinux)
      [[ "$version" =~ ^vmlinux-v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-preview\.[0-9]{8})?$ ]] \
        || release_fail "vmlinux version must match vmlinux-vX.Y.Z or vmlinux-vX.Y.Z-preview.YYYYMMDD"
      ;;
    *) release_fail "unknown release unit: $unit" ;;
  esac
}

component_repository() {
  case "$1" in
    accelerator|connector|sandboxer|orchestrator)
      printf '%s/%s\n' "$RELEASE_ORGANIZATION" "$1"
      ;;
    runtime|vmlinux)
      printf '%s/guest-runtime\n' "$RELEASE_ORGANIZATION"
      ;;
    *) release_fail "unknown release unit: $1" ;;
  esac
}

component_archive() {
  local unit="$1" version="$2" target="${3:-$RELEASE_TARGET}"
  [ "$target" = x86_64 ] || release_fail "unsupported release target: $target"
  validate_unit_version "$unit" "$version"
  case "$unit" in
    accelerator|connector|sandboxer|orchestrator)
      printf '%s-%s-linux-%s.tar.gz\n' "$unit" "$version" "$target"
      ;;
    runtime)
      printf 'sandbox-runtime-%s-%s.tar.gz\n' "$target" "${version#runtime-}"
      ;;
    vmlinux)
      printf 'vmlinux-%s-%s.tar.gz\n' "$target" "${version#vmlinux-}"
      ;;
  esac
}

platform_archive() {
  validate_aggregate_version "$1"
  printf 'platform-%s.tar.gz\n' "$1"
}

resolve_selection() {
  local root="$1" version="$2" output="$3"
  validate_aggregate_version "$version"
  python3 "$root/release/selection.py" "$root" "$version" > "$output"
  local expected actual
  expected="$(printf '%s\n' "${RELEASE_UNITS[@]}" | LC_ALL=C sort)"
  actual="$(cut -f1 "$output" | LC_ALL=C sort)"
  [ "$actual" = "$expected" ] || release_fail "selection does not contain the six release units exactly once"
  while IFS=$'\t' read -r unit selected extra; do
    if [ -z "$unit" ] || [ -z "$selected" ] || [ -n "${extra:-}" ]; then
      release_fail "invalid selection row"
    fi
    validate_unit_version "$unit" "$selected"
  done < "$output"
}

previous_release() {
  [ "$#" -eq 2 ] || release_fail "usage: previous_release <platform-root> <release-version>"
  python3 "$1/release/selection.py" "$1" "$2" --previous
}

is_preview() {
  [[ "$1" == *-preview.* ]]
}

github_api() {
  local endpoint="$1"
  curl --fail --show-error --silent \
    --retry 4 --retry-all-errors --connect-timeout 10 --max-time 60 \
    -H "Accept: application/vnd.github+json" \
    -H "Authorization: Bearer ${GH_TOKEN:?GH_TOKEN is required}" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "${GITHUB_API_URL:-https://api.github.com}/$endpoint"
}

github_download_asset() {
  local repository="$1" asset_id="$2" output="$3"
  curl --fail --show-error --silent --location \
    --retry 4 --retry-all-errors --connect-timeout 10 --max-time 900 \
    -H "Accept: application/octet-stream" \
    -H "Authorization: Bearer ${GH_TOKEN:?GH_TOKEN is required}" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "${GITHUB_API_URL:-https://api.github.com}/repos/$repository/releases/assets/$asset_id" \
    > "$output"
}

verify_github_asset() {
  local file="$1" asset="$2"
  local expected_size expected_digest actual_size actual_digest
  expected_size="$(jq -er '.size' <<< "$asset")"
  expected_digest="$(jq -er '.digest | select(test("^sha256:[0-9a-f]{64}$"))' <<< "$asset")"
  actual_size="$(stat -c '%s' "$file")"
  actual_digest="sha256:$(sha256sum "$file" | awk '{print $1}')"
  [ "$actual_size" = "$expected_size" ] \
    || release_fail "GitHub asset size mismatch: $(basename "$file")"
  [ "$actual_digest" = "$expected_digest" ] \
    || release_fail "GitHub asset digest mismatch: $(basename "$file")"
}

assert_safe_output() {
  local output="$1"
  if [ -z "$output" ] || [ "$output" = / ] || [ "$output" = . ]; then
    release_fail "unsafe output directory: $output"
  fi
  [ ! -e "$output" ] || release_fail "output already exists: $output"
}
