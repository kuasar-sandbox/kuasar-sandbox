#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=release/lib.sh
source "$ROOT/release/lib.sh"

REPOSITORY="${GH_REPO:-${GITHUB_REPOSITORY:-kuasar-sandbox/kuasar-sandbox}}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

api_optional() {
  local endpoint="$1" output="$2"
  if gh api "$endpoint" > "$output" 2> "$TMP/api-error"; then
    return 0
  fi
  if grep -q '(HTTP 404)' "$TMP/api-error"; then
    : > "$output"
    return 4
  fi
  cat "$TMP/api-error" >&2
  return 1
}

find_draft_release() {
  local version="$1" output="$2"
  gh api --paginate --slurp "repos/$REPOSITORY/releases?per_page=100" \
    | jq --arg version "$version" '[.[][] | select(.tag_name == $version and .draft == true)]' \
    > "$output"
  [ "$(jq 'length' "$output")" -le 1 ] \
    || release_fail "multiple draft releases use tag $version"
}

wait_for_draft_release() {
  local version="$1" output="$2" attempt
  for attempt in {1..15}; do
    find_draft_release "$version" "$output"
    [ "$(jq 'length' "$output")" -ne 1 ] || return 0
    [ "$attempt" -eq 15 ] || sleep 1
  done
  release_fail "cannot locate newly created draft for $version"
}

check_release() {
  [ "$#" -eq 1 ] || release_fail "usage: publish-release.sh check <release-version>"
  local version="$1"
  validate_aggregate_version "$version"
  if api_optional "repos/$REPOSITORY/releases/tags/$version" "$TMP/release"; then
    [ "$(jq -r .draft "$TMP/release")" = true ] \
      || release_fail "GitHub release already exists: $version"
  else
    local rc=$?
    [ "$rc" -eq 4 ] || exit "$rc"
  fi
}

verify_uploaded_assets() {
  local state="$1" bundle="$2"
  local expected="$TMP/expected-assets" actual="$TMP/actual-assets" file
  : > "$expected"
  while IFS= read -r file; do
    printf '%s\tsha256:%s\t%s\tuploaded\n' \
      "$(basename "$file")" \
      "$(sha256sum "$file" | awk '{print $1}')" \
      "$(stat -c '%s' "$file")" >> "$expected"
  done < <(find "$bundle/assets" -mindepth 1 -maxdepth 1 -type f -print | LC_ALL=C sort)
  jq -r '.assets[] | [.name, .digest, (.size | tostring), .state] | @tsv' "$state" \
    | LC_ALL=C sort > "$actual"
  LC_ALL=C sort -o "$expected" "$expected"
  if ! cmp -s "$expected" "$actual"; then
    echo "release: uploaded asset set does not match the validated aggregate" >&2
    diff -u "$expected" "$actual" >&2 || true
    exit 1
  fi
}

publish_bundle() {
  [ "$#" -eq 4 ] \
    || release_fail "usage: publish-release.sh publish <release-version> <commit> <bundle-dir> <source-ref>"
  local version="$1" commit="$2" bundle="$3" source_ref="$4"
  validate_aggregate_version "$version"
  [[ "$commit" =~ ^[0-9a-f]{40}$ ]] || release_fail "commit must be a full lowercase SHA"
  [[ "$source_ref" = main || "$source_ref" =~ ^release/v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.x$ ]] \
    || release_fail "source-ref must be main or release/vMAJOR.MINOR.x"
  "$ROOT/release/aggregate-release.sh" validate "$version" "$bundle"

  local tag_state="$TMP/tag"
  if api_optional "repos/$REPOSITORY/git/ref/tags/$version" "$tag_state"; then
    [ "$(jq -er '.object.sha' "$tag_state")" = "$commit" ] \
      || release_fail "$version already points to another commit"
  else
    local rc=$?
    [ "$rc" -eq 4 ] || exit "$rc"
    jq -n --arg ref "refs/tags/$version" --arg sha "$commit" '{ref: $ref, sha: $sha}' \
      | gh api --method POST "repos/$REPOSITORY/git/refs" --input - >/dev/null
  fi

  local release_state="$TMP/release"
  if api_optional "repos/$REPOSITORY/releases/tags/$version" "$release_state"; then
    [ "$(jq -r .draft "$release_state")" = true ] \
      || release_fail "$version is already published; refusing to replace it"
  else
    local rc=$?
    [ "$rc" -eq 4 ] || exit "$rc"
  fi

  local drafts="$TMP/drafts"
  find_draft_release "$version" "$drafts"
  local files=() file name existing
  if [ "$(jq 'length' "$drafts")" -eq 1 ]; then
    jq '.[0]' "$drafts" > "$release_state"
    [ "$(jq -er .target_commitish "$release_state")" = "$commit" ] \
      || release_fail "existing draft belongs to another source commit"
    # A retry may fill absent assets, but must never replace same-name bytes.
    while IFS= read -r name; do
      [ -f "$bundle/assets/$name" ] || release_fail "unexpected existing draft asset: $name"
    done < <(jq -r '.assets[].name' "$release_state")
    while IFS= read -r file; do
      name=$(basename "$file")
      existing=$(jq -c --arg name "$name" '[.assets[] | select(.name == $name)]' "$release_state")
      if [ "$(jq length <<< "$existing")" = 0 ]; then
        files+=("$file")
      else
        jq -e --arg digest "sha256:$(sha256sum "$file" | awk '{print $1}')" \
          --argjson size "$(stat -c '%s' "$file")" \
          'length == 1 and .[0].digest == $digest and .[0].size == $size and .[0].state == "uploaded"' \
          <<< "$existing" >/dev/null || release_fail "existing draft asset differs: $name"
      fi
    done < <(find "$bundle/assets" -mindepth 1 -maxdepth 1 -type f -print | LC_ALL=C sort)
  else
    while IFS= read -r file; do files+=("$file"); done \
      < <(find "$bundle/assets" -mindepth 1 -maxdepth 1 -type f -print | LC_ALL=C sort)
  fi

  # Ownership and all existing asset bytes are checked before registry writes.
  # The registry publisher also refuses conflicting existing image tags.
  python3 -B "$ROOT/release/workbench_registry.py" "$bundle" "$version" "$commit"
  if [ "$(jq 'length' "$drafts")" -eq 1 ]; then
    [ "${#files[@]}" -eq 0 ] || gh release upload "$version" "${files[@]}" --repo "$REPOSITORY"
    jq -n --rawfile body "$bundle/release-notes.md" '{body: $body}' \
      | gh api --method PATCH "repos/$REPOSITORY/releases/$(jq -er .id "$release_state")" --input - >/dev/null
  else
    gh release create "$version" "${files[@]}" --repo "$REPOSITORY" --draft --verify-tag \
      --target "$commit" --title "$version" --notes-file "$bundle/release-notes.md" >/dev/null
  fi

  wait_for_draft_release "$version" "$drafts"
  jq '.[0]' "$drafts" > "$release_state"
  verify_uploaded_assets "$release_state" "$bundle"
  local prerelease=false make_latest=false
  is_preview "$version" && prerelease=true
  [ "$prerelease" = true ] || [ "$source_ref" != main ] || make_latest=true
  if [ "$prerelease" = true ]; then
    "$ROOT/release/validate-preview-line.sh" "$version" "$commit"
  fi
  jq -n --argjson prerelease "$prerelease" --argjson make_latest "$make_latest" '
      {draft: false, prerelease: $prerelease,
       make_latest: ($make_latest | tostring)}
    ' \
    | gh api --method PATCH \
      "repos/$REPOSITORY/releases/$(jq -er '.id' "$release_state")" --input - >/dev/null
  gh api "repos/$REPOSITORY/releases/tags/$version" > "$release_state"
  jq -e --arg version "$version" --arg commit "$commit" --argjson prerelease "$prerelease" '
      .tag_name == $version
      and .target_commitish == $commit
      and .draft == false
      and .prerelease == $prerelease
    ' "$release_state" >/dev/null || release_fail "$version was not published as requested"
  echo "==> published $REPOSITORY $version from $commit"
}

[[ "$REPOSITORY" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || release_fail "invalid repository"
command -v gh >/dev/null || release_fail "gh is required"
command -v jq >/dev/null || release_fail "jq is required"

case "${1:-}" in
  check) shift; check_release "$@" ;;
  publish) shift; publish_bundle "$@" ;;
  *) release_fail "usage: publish-release.sh <check|publish> ..." ;;
esac
