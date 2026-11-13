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
  local expected="$TMP/expected-assets" actual="$TMP/actual-assets"
  # Compare remote bytes to the accepted plan, not a newly hashed upload tree.
  jq -r '.baseline.assets[] | [.name, .digest, (.size | tostring), "uploaded"] | @tsv' \
    "$RELEASE_PLAN_FILE" | LC_ALL=C sort > "$expected"
  jq -e '[.assets[].id] | all(type == "number" and . > 0) and length == (unique | length)' \
    "$state" >/dev/null || release_fail "uploaded assets have missing or duplicate IDs"
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
  "$ROOT/release/aggregate-release.sh" validate "$version" "$bundle" --publication
  python3 -B "$ROOT/release/publication_body.py" "$bundle/release-notes.md"
  : "${RELEASE_PLAN_FILE:?publication requires its original accepted plan}"
  : "${RELEASE_VALIDATION_FILE:?publication requires successful stage validation}"
  : "${RELEASE_WORKBENCH_RESULTS:?publication requires this stage Workbench results}"
  local stage_inputs=(--plan "$RELEASE_PLAN_FILE" --validation "$RELEASE_VALIDATION_FILE" --workbench "$RELEASE_WORKBENCH_RESULTS")
  python3 -B "$ROOT/release/workbench_registry.py" "$bundle" "$version" "$commit" --validate-only "${stage_inputs[@]}"
  local node_infra=false
  if [[ "$(release_delivery "${PLATFORM_SOURCE_ROOT:-$ROOT}" "$version")" = node-infra-v[12] ]]; then
    node_infra=true
    python3 -B "$ROOT/release/node_infra_registry.py" "$bundle" "$version" "$commit" --validate-only "${stage_inputs[@]}"
  fi

  # A new Stable root tag must not exist while its public assets are incomplete.
  # GitHub binds a draft release's exact --target commit when it is published;
  # only legacy/Preview publication uses the earlier explicit tag.
  local tag_state="$TMP/tag" tag_exists=false
  if api_optional "repos/$REPOSITORY/git/ref/tags/$version" "$tag_state"; then
    local resolved_commit
    resolved_commit=$(gh api "repos/$REPOSITORY/commits/$version" --jq .sha)
    [ "$resolved_commit" = "$commit" ] \
      || release_fail "$version already points to another commit"
    tag_exists=true
  else
    local rc=$?
    [ "$rc" -eq 4 ] || exit "$rc"
    if is_preview "$version"; then
      jq -n --arg ref "refs/tags/$version" --arg sha "$commit" '{ref: $ref, sha: $sha}' \
        | gh api --method POST "repos/$REPOSITORY/git/refs" --input - >/dev/null
      tag_exists=true
    fi
  fi

  local release_state="$TMP/release"
  if api_optional "repos/$REPOSITORY/releases/tags/$version" "$release_state"; then
    if [ "$(jq -r .draft "$release_state")" = false ]; then
      # PATCH may have succeeded before the previous job lost its connection.
      # An accepted, unchanged stage can confirm completion without mutation.
      [ "$tag_exists" = true ] || release_fail "published Release lacks its selected tag"
      local expected_preview=false
      is_preview "$version" && expected_preview=true
      verify_uploaded_assets "$release_state" "$bundle"
      jq -e --arg version "$version" --argjson preview "$expected_preview" \
        --rawfile body "$bundle/release-notes.md" \
        '.tag_name == $version and .prerelease == $preview and .body == $body' \
        "$release_state" >/dev/null || release_fail "published Release differs from the original stage; refusing to replace it"
      python3 -B "$ROOT/release/workbench_registry.py" "$bundle" "$version" "$commit" --read-only "${stage_inputs[@]}"
      if [ "$node_infra" = true ]; then
        python3 -B "$ROOT/release/node_infra_registry.py" "$bundle" "$version" "$commit" --read-only "${stage_inputs[@]}"
      fi
      echo "==> verified already published $REPOSITORY $version from its original accepted stage"
      return
    fi
  else
    local rc=$?
    [ "$rc" -eq 4 ] || exit "$rc"
  fi

  local drafts="$TMP/drafts"
  find_draft_release "$version" "$drafts"
  local files=() file name existing
  if [ "$(jq 'length' "$drafts")" -eq 1 ]; then
    jq '.[0]' "$drafts" > "$release_state"
    [ "$(jq -er .tag_name "$release_state")" = "$version" ] \
      || release_fail "existing draft belongs to another version"
    # An untagged Stable draft must be pinned to the admitted source before
    # assets are resumed; publication will create its root tag from this value.
    if [ "$tag_exists" = false ] && ! is_preview "$version"; then
      [ "$(jq -er .target_commitish "$release_state")" = "$commit" ] \
        || release_fail "existing Stable draft targets another commit"
    fi
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
  python3 -B "$ROOT/release/workbench_registry.py" "$bundle" "$version" "$commit" "${stage_inputs[@]}"
  if [ "$node_infra" = true ]; then
    python3 -B "$ROOT/release/node_infra_registry.py" "$bundle" "$version" "$commit" "${stage_inputs[@]}"
  fi
  python3 -B "$ROOT/release/publication_body.py" "$bundle/release-notes.md"
  if [ "$(jq 'length' "$drafts")" -eq 1 ]; then
    [ "${#files[@]}" -eq 0 ] || gh release upload "$version" "${files[@]}" --repo "$REPOSITORY"
    jq -n --rawfile body "$bundle/release-notes.md" '{body: $body}' \
      | gh api --method PATCH "repos/$REPOSITORY/releases/$(jq -er .id "$release_state")" --input - >/dev/null
  else
    local verify_tag=()
    [ "$tag_exists" = false ] || verify_tag=(--verify-tag)
    gh release create "$version" "${files[@]}" --repo "$REPOSITORY" --draft "${verify_tag[@]}" \
      --target "$commit" --title "$version" --notes-file "$bundle/release-notes.md" >/dev/null
  fi

  wait_for_draft_release "$version" "$drafts"
  jq '.[0]' "$drafts" > "$release_state"
  verify_uploaded_assets "$release_state" "$bundle"
  local prerelease=false make_latest=false
  is_preview "$version" && prerelease=true
  [ "$prerelease" = true ] || [ "$source_ref" != main ] || make_latest=true
  if [ "$prerelease" = true ]; then
    : "${PLATFORM_SOURCE_ROOT:?Preview publication requires the admitted platform source artifact}"
    "$ROOT/release/validate-preview-line.sh" "$version" "$commit"
  fi
  jq -n --argjson prerelease "$prerelease" --argjson make_latest "$make_latest" '
      {draft: false, prerelease: $prerelease,
       make_latest: ($make_latest | tostring)}
    ' \
    | gh api --method PATCH \
      "repos/$REPOSITORY/releases/$(jq -er '.id' "$release_state")" --input - >/dev/null
  gh api "repos/$REPOSITORY/releases/tags/$version" > "$release_state"
  jq -e --arg version "$version" --rawfile body "$bundle/release-notes.md" --argjson prerelease "$prerelease" '
      .tag_name == $version
      and .body == $body
      and .draft == false
      and .prerelease == $prerelease
    ' "$release_state" >/dev/null || release_fail "$version was not published as requested"
  verify_uploaded_assets "$release_state" "$bundle"
  # The public Release and its complete verified asset set must now identify
  # the selected commit through the immutable Stable (or Preview) tag.
  local published_tag_commit
  published_tag_commit=$(gh api "repos/$REPOSITORY/commits/$version" --jq .sha) \
    || release_fail "published $version has no resolvable tag"
  [ "$published_tag_commit" = "$commit" ] \
    || release_fail "published $version tag does not identify the admitted source"
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
