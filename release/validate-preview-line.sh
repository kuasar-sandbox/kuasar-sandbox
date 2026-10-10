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

[[ "$TAG" =~ ^release-v[0-9]+\.[0-9]+\.[0-9]+-preview\.[0-9]{8}(\.[1-9][0-9]*)?$ ]] || {
  echo "aggregate Preview must match release-vX.Y.Z-preview.YYYYMMDD[.N]" >&2
  exit 1
}
[[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] || {
  echo "source-sha must be a full lowercase SHA" >&2
  exit 1
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
if [ -n "${PLATFORM_SOURCE_ROOT:-}" ]; then
  # The aggregate run already admitted and transported this exact checkout.
  python3 -B - "$(dirname "${BASH_SOURCE[0]}")/../ci/integration" "$PLATFORM_SOURCE_ROOT" "$SOURCE_SHA" "$TMP/daily-preview.yaml" <<'PYCODE'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
import source_inputs
root = Path(sys.argv[2])
source_inputs.inspect_platform(root, sys.argv[3])
text = (root / 'releases/daily-preview.yaml').read_text()
source_inputs.tag_sources.selection.parse_manifest(text, 'admitted Preview', True)
Path(sys.argv[4]).write_text(text)
PYCODE
else
# The commit is observed evidence, never a Contents retrieval key. Admit a
# trusted branch by identity and reject movement around the manifest read.
aggregate_line="${TAG#release-v}"
aggregate_line="${aggregate_line%%-preview.*}"
aggregate_line="${aggregate_line%.*}"
manifest_ref=
for branch in main "release/v$aggregate_line.x"; do
  if observed=$(gh api "repos/kuasar-sandbox/kuasar-sandbox/git/ref/heads/$branch" \
      --jq 'select(.object.type == "commit") | .object.sha' 2> "$TMP/ref.error"); then
    if [ "$observed" = "$SOURCE_SHA" ]; then manifest_ref=$branch; break; fi
  elif ! grep -q '(HTTP 404)' "$TMP/ref.error"; then
    cat "$TMP/ref.error" >&2; exit 1
  fi
done
[ -n "$manifest_ref" ] || { echo 'aggregate identity is not an admitted branch HEAD' >&2; exit 1; }
gh api \
  "repos/kuasar-sandbox/kuasar-sandbox/contents/releases/daily-preview.yaml?ref=$manifest_ref" \
  --jq .content | tr -d '\n' | base64 -d > "$TMP/daily-preview.yaml"
[ "$(gh api "repos/kuasar-sandbox/kuasar-sandbox/git/ref/heads/$manifest_ref" \
    --jq 'select(.object.type == "commit") | .object.sha')" = "$SOURCE_SHA" ] \
  || { echo 'aggregate branch moved during manifest admission' >&2; exit 1; }

fi

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
