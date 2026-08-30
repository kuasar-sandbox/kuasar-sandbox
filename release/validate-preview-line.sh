#!/usr/bin/env bash

set -euo pipefail

[ "$#" -eq 2 ] || {
  echo "usage: validate-preview-line.sh <aggregate-tag> <source-sha>" >&2
  exit 2
}

TAG="$1"
SOURCE_SHA="$2"
if [[ "$TAG" != *-preview.* ]]; then
  exit 0
fi

[[ "$TAG" =~ ^release-v[0-9]+\.[0-9]+\.[0-9]+-preview\.[0-9]{8}$ ]] || {
  echo "aggregate Preview must match release-vX.Y.Z-preview.YYYYMMDD" >&2
  exit 1
}
[[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] || {
  echo "source-sha must be a full lowercase SHA" >&2
  exit 1
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
gh api \
  "repos/kuasar-sandbox/kuasar-sandbox/contents/releases/daily-preview.yaml?ref=$SOURCE_SHA" \
  --jq .content | tr -d '\n' | base64 -d > "$TMP/daily-preview.yaml"

BASE_COUNT="$(awk '/^version:[[:space:]]+/ {count++} END {print count + 0}' \
  "$TMP/daily-preview.yaml")"
PREVIEW_COUNT="$(awk '/^preview_version:[[:space:]]+/ {count++} END {print count + 0}' \
  "$TMP/daily-preview.yaml")"
if [ "$BASE_COUNT" -ne 1 ] || [ "$PREVIEW_COUNT" -ne 1 ]; then
  echo "platform commit $SOURCE_SHA has an invalid Daily Preview manifest" >&2
  exit 1
fi
BASE="$(awk '/^version:[[:space:]]+/ {print $2}' "$TMP/daily-preview.yaml")"
PREVIEW="$(awk '/^preview_version:[[:space:]]+/ {print $2}' "$TMP/daily-preview.yaml")"
[ "$BASE-$PREVIEW" = "$TAG" ] || {
  echo "$TAG is not selected by platform commit $SOURCE_SHA" >&2
  exit 1
}

STABLE="${TAG%-preview.*}"
if gh api "repos/kuasar-sandbox/kuasar-sandbox/releases/tags/$STABLE" \
  > "$TMP/stable" 2> "$TMP/stable.error"; then
  echo "$STABLE is closed; refusing to recreate an aggregate Preview" >&2
  exit 1
fi
if ! grep -q '(HTTP 404)' "$TMP/stable.error"; then
  cat "$TMP/stable.error" >&2
  exit 1
fi
