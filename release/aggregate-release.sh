#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=release/lib.sh
source "$ROOT/release/lib.sh"

fetch_components() {
  [ "$#" -eq 2 ] || release_fail "usage: aggregate-release.sh fetch <release-version> <output-dir>"
  local version="$1" output="$2"
  assert_safe_output "$output"
  mkdir -p "$output/components"
  resolve_selection "$ROOT" "$version" "$output/selection.tsv"

  local unit tag repository archive release_state expected_prerelease unit_dir
  while IFS=$'\t' read -r unit tag; do
    repository="$(component_repository "$unit")"
    archive="$(component_archive "$unit" "$tag")"
    unit_dir="$output/components/$unit"
    mkdir -p "$unit_dir"
    release_state="$(github_api "repos/$repository/releases/tags/$tag")"
    expected_prerelease=false
    is_preview "$tag" && expected_prerelease=true
    jq -e \
      --arg tag "$tag" \
      --arg archive "$archive" \
      --argjson prerelease "$expected_prerelease" '
        .tag_name == $tag
        and .draft == false
        and .prerelease == $prerelease
        and (.assets | length == 2)
        and ([.assets[].name] | sort == (["SHA256SUMS", $archive] | sort))
        and ([.assets[].digest] | all(type == "string" and test("^sha256:[0-9a-f]{64}$")))
      ' <<< "$release_state" >/dev/null \
      || release_fail "$repository $tag does not satisfy the two-asset release contract"

    local name asset
    for name in "$archive" SHA256SUMS; do
      asset="$(jq -ce --arg name "$name" '.assets[] | select(.name == $name)' <<< "$release_state")"
      github_download_asset "$repository" "$(jq -er '.id' <<< "$asset")" "$unit_dir/$name"
      verify_github_asset "$unit_dir/$name" "$asset"
    done
    validate_component_download "$unit" "$tag" "$unit_dir"
  done < "$output/selection.tsv"
}

validate_component_download() {
  local unit="$1" tag="$2" directory="$3"
  local archive checksum_line checksum_name checksum_value
  archive="$(component_archive "$unit" "$tag")"
  [ -f "$directory/$archive" ] || release_fail "$unit archive is missing"
  [ -f "$directory/SHA256SUMS" ] || release_fail "$unit SHA256SUMS is missing"
  [ "$(grep -cve '^[[:space:]]*$' "$directory/SHA256SUMS")" -eq 1 ] \
    || release_fail "$unit SHA256SUMS must contain exactly one entry"
  checksum_line="$(grep -ve '^[[:space:]]*$' "$directory/SHA256SUMS")"
  read -r checksum_value checksum_name _ <<< "$checksum_line"
  checksum_name="${checksum_name#\*}"
  [ "$checksum_name" = "$archive" ] || release_fail "$unit SHA256SUMS names an unexpected asset"
  [[ "$checksum_value" =~ ^[0-9a-f]{64}$ ]] || release_fail "$unit SHA256SUMS contains an invalid digest"
  [ "$(sha256sum "$directory/$archive" | awk '{print $1}')" = "$checksum_value" ] \
    || release_fail "$unit archive checksum mismatch"
}

validate_tar_paths() {
  local archive="$1" seen="$2"
  local listing
  listing="$(mktemp)"
  tar -tzf "$archive" > "$listing"
  local raw path
  while IFS= read -r raw; do
    path="${raw#./}"
    [ -n "$path" ] || continue
    [[ "$path" != /* && "$path" != ../* && "$path" != */../* && "$path" != */.. ]] \
      || release_fail "unsafe path in $(basename "$archive"): $raw"
    [[ "$path" == */ ]] && continue
    if grep -Fx "$path" "$seen" >/dev/null; then
      release_fail "release archives contain the same file path: $path"
    fi
    printf '%s\n' "$path" >> "$seen"
  done < "$listing"
  rm -f "$listing"
}

assemble_release() {
  [ "$#" -eq 3 ] || release_fail "usage: aggregate-release.sh assemble <release-version> <fetched-dir> <output-dir>"
  local version="$1" fetched="$2" output="$3"
  validate_aggregate_version "$version"
  [ -d "$fetched/components" ] || release_fail "fetched component directory is missing"
  assert_safe_output "$output"

  local work expected_selection platform_bundle platform_name
  work="$(mktemp -d)"
  expected_selection="$work/selection.tsv"
  resolve_selection "$ROOT" "$version" "$expected_selection"
  cmp -s "$expected_selection" "$fetched/selection.tsv" \
    || release_fail "fetched component selection does not match platform main"

  platform_bundle="$work/platform-bundle"
  "$ROOT/release/package-platform.sh" package "$version" "$platform_bundle"
  platform_name="$(platform_archive "$version")"
  mkdir -p "$output/assets"
  install -m 0644 "$platform_bundle/assets/$platform_name" "$output/assets/$platform_name"
  install -m 0644 "$expected_selection" "$output/selection.tsv"

  local unit tag archive
  while IFS=$'\t' read -r unit tag; do
    validate_component_download "$unit" "$tag" "$fetched/components/$unit"
    archive="$(component_archive "$unit" "$tag")"
    install -m 0644 "$fetched/components/$unit/$archive" "$output/assets/$archive"
  done < "$expected_selection"

  find "$output/assets" -maxdepth 1 -type f -printf '%f\n' \
    | LC_ALL=C sort > "$work/asset-names"
  (cd "$output/assets" && xargs sha256sum < "$work/asset-names") > "$output/assets/SHA256SUMS"
  write_release_notes "$version" "$expected_selection" "$output/release-notes.md"
  validate_bundle "$version" "$output"
  rm -rf "$work"
}

write_release_notes() {
  local version="$1" selection="$2" output="$3"
  {
    printf 'Kuasar Sandbox %s.\n\n' "$version"
    printf 'This aggregate contains the platform documentation/test package and the exact component archives validated together on BMS.\n\n'
    printf 'Component versions:\n\n'
    while IFS=$'\t' read -r unit tag; do
      printf -- "- \`%s\`: \`%s\`\n" "$unit" "$tag"
    done < "$selection"
    printf "\nVerify every explicit asset with \`SHA256SUMS\`. GitHub provides source archives automatically for this tag.\n"
  } > "$output"
}

expected_asset_names() {
  local version="$1" selection="$2"
  platform_archive "$version"
  while IFS=$'\t' read -r unit tag; do
    component_archive "$unit" "$tag"
  done < "$selection"
  printf 'SHA256SUMS\n'
}

validate_bundle() {
  [ "$#" -eq 2 ] || release_fail "usage: aggregate-release.sh validate <release-version> <bundle-dir>"
  local version="$1" bundle="$2"
  validate_aggregate_version "$version"
  [ -d "$bundle/assets" ] || release_fail "aggregate assets directory is missing"
  [ -s "$bundle/release-notes.md" ] || release_fail "aggregate release notes are missing"
  [ -f "$bundle/selection.tsv" ] || release_fail "aggregate selection is missing"
  local expected_selection expected_names actual_names
  expected_selection="$(mktemp)"
  expected_names="$(mktemp)"
  actual_names="$(mktemp)"
  resolve_selection "$ROOT" "$version" "$expected_selection"
  cmp -s "$expected_selection" "$bundle/selection.tsv" \
    || release_fail "aggregate selection does not match platform main"
  expected_asset_names "$version" "$expected_selection" | LC_ALL=C sort > "$expected_names"
  find "$bundle/assets" -mindepth 1 -maxdepth 1 -type f -printf '%f\n' | LC_ALL=C sort > "$actual_names"
  cmp -s "$expected_names" "$actual_names" \
    || { diff -u "$expected_names" "$actual_names" >&2 || true; release_fail "aggregate contains an unexpected asset set"; }
  (cd "$bundle/assets" && sha256sum --quiet -c SHA256SUMS) \
    || release_fail "aggregate SHA256SUMS validation failed"
  "$ROOT/release/package-platform.sh" validate "$version" \
    "$bundle/assets/$(platform_archive "$version")"

  local seen unit tag archive
  seen="$(mktemp)"
  : > "$seen"
  validate_tar_paths "$bundle/assets/$(platform_archive "$version")" "$seen"
  while IFS=$'\t' read -r unit tag; do
    archive="$(component_archive "$unit" "$tag")"
    validate_tar_paths "$bundle/assets/$archive" "$seen"
  done < "$expected_selection"
  rm -f "$seen"
  rm -f "$expected_selection" "$expected_names" "$actual_names"
}

extract_bundle() {
  [ "$#" -eq 3 ] || release_fail "usage: aggregate-release.sh extract <release-version> <bundle-dir> <install-dir>"
  local version="$1" bundle="$2" install="$3"
  validate_bundle "$version" "$bundle"
  assert_safe_output "$install"
  mkdir -p "$install"
  local name
  while IFS= read -r name; do
    [ "$name" = SHA256SUMS ] && continue
    tar -xzf "$bundle/assets/$name" -C "$install"
  done < <(expected_asset_names "$version" "$bundle/selection.tsv")
}

case "${1:-}" in
  fetch) shift; fetch_components "$@" ;;
  assemble) shift; assemble_release "$@" ;;
  validate) shift; validate_bundle "$@" ;;
  extract) shift; extract_bundle "$@" ;;
  *) release_fail "usage: aggregate-release.sh <fetch|assemble|validate|extract> ..." ;;
esac
