#!/usr/bin/env bash

set -euo pipefail

[ "$#" -eq 3 ] || {
  echo "usage: delete-preview.sh <tag> <source-sha> <incomplete|gc>" >&2
  exit 2
}

TAG="$1"
SOURCE_SHA="$2"
MODE="$3"
REPOSITORY="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=release/lib.sh
source "$ROOT/release/lib.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail() {
  echo "delete-preview: $*" >&2
  exit 1
}

[[ "$TAG" =~ ^release-v[0-9]+\.[0-9]+\.[0-9]+-preview\.[0-9]{8}$ ]] \
  || fail "invalid aggregate Preview tag: $TAG"
[[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "source-sha must be a full lowercase SHA"
[ "$MODE" = incomplete ] || [ "$MODE" = gc ] || fail "mode must be incomplete or gc"

gh api \
  "repos/$REPOSITORY/contents/releases/daily-preview.yaml?ref=$SOURCE_SHA" \
  --jq .content | tr -d '\n' | base64 -d > "$TMP/daily-preview.yaml"
BASE="$(awk '/^version:[[:space:]]+/ {print $2}' "$TMP/daily-preview.yaml")"
PREVIEW="$(awk '/^preview_version:[[:space:]]+/ {print $2}' \
  "$TMP/daily-preview.yaml")"
if [ "$BASE-$PREVIEW" = "$TAG" ]; then
  [ "$(awk '/^components:[[:space:]]*$/ {count++} END {print count + 0}' \
    "$TMP/daily-preview.yaml")" -eq 1 ] \
    || fail "manifest must contain components exactly once"
  : > "$TMP/selection.tsv"
  for unit in "${RELEASE_UNITS[@]}"; do
    selected="$(awk -v key="$unit:" '
      /^components:[[:space:]]*$/ {inside = 1; next}
      /^[^[:space:]]/ {inside = 0}
      inside && substr($0, 1, 2) == "  " &&
        substr($0, 3, 1) !~ /[[:space:]]/ && $1 == key {print $2}
    ' "$TMP/daily-preview.yaml")"
    [ "$(awk -v key="$unit:" '
      /^components:[[:space:]]*$/ {inside = 1; next}
      /^[^[:space:]]/ {inside = 0}
      inside && substr($0, 1, 2) == "  " &&
        substr($0, 3, 1) !~ /[[:space:]]/ && $1 == key {count++}
      END {print count + 0}
    ' "$TMP/daily-preview.yaml")" -eq 1 ] \
      || fail "manifest must select $unit exactly once"
    validate_unit_version "$unit" "$selected"
    printf '%s\t%s\n' "$unit" "$selected" >> "$TMP/selection.tsv"
  done
elif [ "$MODE" = gc ]; then
  # Early aggregate Previews predate exact manifest-commit tagging. Their
  # ownership is still provable from the canonical first-parent manifest
  # history used to build the GC plan.
  resolve_selection "$ROOT" "$TAG" "$TMP/selection.tsv"
  CANONICAL_COMMIT="$("$ROOT/release/selection.py" "$ROOT" "$TAG" --commit)"
  [[ "$CANONICAL_COMMIT" =~ ^[0-9a-f]{40}$ ]] \
    || fail "cannot resolve canonical manifest commit for $TAG"
  if [ -n "$PREVIEW" ]; then
    fail "$SOURCE_SHA selects another aggregate Preview"
  fi
  git -C "$ROOT" rev-list --first-parent "$CANONICAL_COMMIT" \
    | grep -Fx "$SOURCE_SHA" >/dev/null \
    || fail "$SOURCE_SHA is outside the canonical first-parent history for $TAG"
else
  fail "$SOURCE_SHA does not select aggregate Preview $TAG"
fi
{
  printf '%s\n' SHA256SUMS "$(platform_archive "$TAG")"
  while IFS=$'\t' read -r unit selected; do
    component_archive "$unit" "$selected"
  done < "$TMP/selection.tsv"
} | LC_ALL=C sort > "$TMP/expected-assets"

gh api --paginate --slurp "repos/$REPOSITORY/releases?per_page=100" \
  | jq --arg tag "$TAG" '[.[][] | select(.tag_name == $tag)]' > "$TMP/releases"
[ "$(jq 'length' "$TMP/releases")" -le 1 ] || fail "multiple releases use $TAG"

if [ "$(jq 'length' "$TMP/releases")" -eq 1 ]; then
  jq '.[0]' "$TMP/releases" > "$TMP/release"
  jq -e '.prerelease == true or .draft == true' "$TMP/release" >/dev/null \
    || fail "refusing to delete a non-preview release"
  complete=false
  if jq -e '
      .draft == false
      and .prerelease == true
      and (.assets | length == 8)
      and all(.assets[]; .state == "uploaded")
    ' "$TMP/release" >/dev/null \
    && jq -r '.assets[].name' "$TMP/release" | LC_ALL=C sort \
      | cmp -s "$TMP/expected-assets" -; then
    complete=true
  fi
  if [ "$MODE" = incomplete ] && [ "$complete" = true ]; then
    fail "refusing incomplete recovery for a complete aggregate Preview"
  fi
fi

if [ "$(jq 'length' "$TMP/releases")" -eq 1 ]; then
  jq -e --arg source_sha "$SOURCE_SHA" \
    '.[0].target_commitish == $source_sha' "$TMP/releases" >/dev/null \
    || fail "Release target_commitish does not match the Preview Tag"
fi

if gh api "repos/$REPOSITORY/git/ref/tags/$TAG" > "$TMP/ref" 2> "$TMP/ref-error"; then
  [ "$(jq -er '.object.type' "$TMP/ref")" = commit ] \
    || fail "refusing to delete a non-lightweight tag"
  [ "$(jq -er '.object.sha' "$TMP/ref")" = "$SOURCE_SHA" ] \
    || fail "$TAG does not point to the expected platform commit"
elif grep -q '(HTTP 404)' "$TMP/ref-error"; then
  : > "$TMP/ref"
else
  cat "$TMP/ref-error" >&2
  exit 1
fi

if [ "$(jq 'length' "$TMP/releases")" -eq 1 ]; then
  gh api --method DELETE \
    "repos/$REPOSITORY/releases/$(jq -er '.[0].id' "$TMP/releases")" --silent
fi
if [ -s "$TMP/ref" ]; then
  gh api --method DELETE "repos/$REPOSITORY/git/refs/tags/$TAG" --silent
fi

echo "==> deleted $REPOSITORY $TAG release/assets/tag ($MODE)"
