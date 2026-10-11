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

[[ "$TAG" =~ ^release-v[0-9]+\.[0-9]+\.[0-9]+-preview\.[0-9]{8}(\.[1-9][0-9]*)?$ ]] \
  || fail "invalid aggregate Preview tag: $TAG"
[[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "source-sha must be a full lowercase SHA"
[ "$MODE" = incomplete ] || [ "$MODE" = gc ] || fail "mode must be incomplete or gc"

# Released records are read by tag. An interrupted tagless publication may
# recover only while its recorded identity is still on a trusted branch.
manifest_ref=$TAG
if observed=$(gh api "repos/$REPOSITORY/git/ref/tags/$TAG" \
    --jq 'select(.object.type == "commit") | .object.sha' 2> "$TMP/ref.error"); then
  [ "$observed" = "$SOURCE_SHA" ] || fail 'tag source identity changed'
  identity_path="git/ref/tags/$TAG"
else
  grep -q '(HTTP 404)' "$TMP/ref.error" || { cat "$TMP/ref.error" >&2; exit 1; }
  line="${TAG#release-v}"; line="${line%%-preview.*}"; line="${line%.*}"
  manifest_ref=
  for branch in main "release/v$line.x"; do
    if observed=$(gh api "repos/$REPOSITORY/git/ref/heads/$branch" \
        --jq 'select(.object.type == "commit") | .object.sha' 2> "$TMP/ref.error"); then
      if [ "$observed" = "$SOURCE_SHA" ]; then manifest_ref=$branch; break; fi
    elif ! grep -q '(HTTP 404)' "$TMP/ref.error"; then
      cat "$TMP/ref.error" >&2; exit 1
    fi
  done
  [ -n "$manifest_ref" ] || fail 'tagless publication source is no longer an admitted branch HEAD'
  identity_path="git/ref/heads/$manifest_ref"
fi
gh api \
  "repos/$REPOSITORY/contents/releases/daily-preview.yaml?ref=$manifest_ref" \
  --jq .content | tr -d '\n' | base64 -d > "$TMP/daily-preview.yaml"
[ "$(gh api "repos/$REPOSITORY/$identity_path" \
    --jq 'select(.object.type == "commit") | .object.sha')" = "$SOURCE_SHA" ] \
  || fail 'manifest source moved during admission'

BASE="$(awk '/^version:[[:space:]]+/ {print $2}' "$TMP/daily-preview.yaml")"
PREVIEW="$(awk '/^preview_version:[[:space:]]+/ {print $2}' \
  "$TMP/daily-preview.yaml")"
DELIVERY=$(python3 - "$ROOT/release" "$TMP/daily-preview.yaml" <<'PYDELIVERY'
import pathlib, sys
sys.path.insert(0, sys.argv[1])
import selection
p=pathlib.Path(sys.argv[2])
print(selection.delivery(selection.read_simple_yaml(p.read_text(), str(p)), str(p)))
PYDELIVERY
)
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
  DELIVERY=$(release_delivery "$ROOT" "$TAG")
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
{
  cat "$TMP/expected-assets"
  while IFS=$'\t' read -r unit selected; do
    component_archive "$unit" "$selected" aarch64
  done < "$TMP/selection.tsv"
} | LC_ALL=C sort > "$TMP/expected-dual-assets"
if [ "$DELIVERY" = workbench-v1 ]; then
  for arch in x86_64 aarch64; do workbench_archive "$TAG" "$arch"; done >> "$TMP/expected-dual-assets"
  LC_ALL=C sort -o "$TMP/expected-dual-assets" "$TMP/expected-dual-assets"
  cp "$TMP/expected-dual-assets" "$TMP/expected-assets"
fi

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
      and ((.assets | length) == ([.assets[].name] | unique | length))
      and all(.assets[]; .state == "uploaded")
    ' "$TMP/release" >/dev/null; then
    jq -r '.assets[].name' "$TMP/release" | LC_ALL=C sort > "$TMP/actual-assets"
    if cmp -s "$TMP/expected-assets" "$TMP/actual-assets" || cmp -s "$TMP/expected-dual-assets" "$TMP/actual-assets"; then
      complete=true
    fi
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

if [ "$DELIVERY" = workbench-v1 ]; then
  python3 -B "$ROOT/release/workbench_gc.py" "$TAG" "$SOURCE_SHA"
fi

if [ "$(jq 'length' "$TMP/releases")" -eq 1 ]; then
  gh api --method DELETE \
    "repos/$REPOSITORY/releases/$(jq -er '.[0].id' "$TMP/releases")" --silent
fi
if [ -s "$TMP/ref" ]; then
  gh api --method DELETE "repos/$REPOSITORY/git/refs/tags/$TAG" --silent
fi

echo "==> deleted $REPOSITORY $TAG release/assets/tag ($MODE)"
