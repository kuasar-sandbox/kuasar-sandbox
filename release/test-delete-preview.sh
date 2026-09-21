#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"
cat > "$TMP/bin/gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == *"contents/releases/daily-preview.yaml"* ]]; then
  printf '%s\n' "${FAKE_MANIFEST:?}"
  exit 0
fi
if [[ "$*" == *"releases?per_page=100"* ]]; then
  if [ -n "${FAKE_COMPLETE_ASSETS:-}" ]; then
    jq -cn --arg tag "$FAKE_TAG" --arg target "$FAKE_TARGET" --argjson names "$FAKE_COMPLETE_ASSETS" \
      '[[{id:17,tag_name:$tag,target_commitish:$target,draft:false,prerelease:true,assets:[$names[] | {name:.,state:"uploaded"}]}]]'
    exit 0
  fi
  jq -cn --arg tag "${FAKE_TAG:?}" --arg target "${FAKE_TARGET:?}" \
    '[[{id: 17, tag_name: $tag, target_commitish: $target,
        draft: true, prerelease: false, assets: []}]]'
  exit 0
fi
if [[ "$*" == *"/git/ref/tags/"* ]]; then
  echo 'gh: Not Found (HTTP 404)' >&2
  exit 1
fi
if [[ "$*" == *"--method DELETE"* ]]; then
  printf '%s\n' "$*" >> "${FAKE_DELETE_LOG:?}"
  exit 0
fi
echo "unexpected gh invocation: $*" >&2
exit 1
EOF
chmod +x "$TMP/bin/gh"

TAG=release-v9.8.7-preview.20260831
SOURCE_SHA=1111111111111111111111111111111111111111
DELETE_LOG="$TMP/deletes"
FAKE_MANIFEST="$(printf '%s\n' \
  'version: release-v9.8.7' \
  'preview_version: preview.20260831' \
  'components:' \
  '  accelerator: v1.0.1-preview.20260831' \
  '  connector: v1.0.2-preview.20260831' \
  '  sandboxer: v1.0.3-preview.20260831' \
  '  orchestrator: v1.0.4-preview.20260831' \
  '  runtime: runtime-v1.0.5-preview.20260831' \
  '  vmlinux: vmlinux-v1.0.6-preview.20260831' | base64 -w0)"

if PATH="$TMP/bin:$PATH" GITHUB_REPOSITORY=kuasar-sandbox/kuasar-sandbox \
  FAKE_TAG="$TAG" FAKE_TARGET=2222222222222222222222222222222222222222 \
  FAKE_DELETE_LOG="$DELETE_LOG" FAKE_MANIFEST="$FAKE_MANIFEST" \
  bash "$SCRIPT_DIR/delete-preview.sh" "$TAG" "$SOURCE_SHA" incomplete \
  >/dev/null 2>&1; then
  echo "test-delete-preview: deleted a Release without source ownership" >&2
  exit 1
fi
[ ! -e "$DELETE_LOG" ]

OUTSIDE_COMPONENT_MANIFEST="$(printf '%s\n' \
  'version: release-v9.8.7' \
  'preview_version: preview.20260831' \
  'metadata:' \
  '  accelerator: v1.0.1-preview.20260831' \
  'components:' \
  '  connector: v1.0.2-preview.20260831' \
  '  sandboxer: v1.0.3-preview.20260831' \
  '  orchestrator: v1.0.4-preview.20260831' \
  '  runtime: runtime-v1.0.5-preview.20260831' \
  '  vmlinux: vmlinux-v1.0.6-preview.20260831' | base64 -w0)"
if PATH="$TMP/bin:$PATH" GITHUB_REPOSITORY=kuasar-sandbox/kuasar-sandbox \
  FAKE_TAG="$TAG" FAKE_TARGET="$SOURCE_SHA" FAKE_DELETE_LOG="$DELETE_LOG" \
  FAKE_MANIFEST="$OUTSIDE_COMPONENT_MANIFEST" \
  bash "$SCRIPT_DIR/delete-preview.sh" "$TAG" "$SOURCE_SHA" incomplete \
  >/dev/null 2>&1; then
  echo "test-delete-preview: accepted a unit outside components" >&2
  exit 1
fi

PATH="$TMP/bin:$PATH" GITHUB_REPOSITORY=kuasar-sandbox/kuasar-sandbox \
  FAKE_TAG="$TAG" FAKE_TARGET="$SOURCE_SHA" FAKE_DELETE_LOG="$DELETE_LOG" \
  FAKE_MANIFEST="$FAKE_MANIFEST" \
  bash "$SCRIPT_DIR/delete-preview.sh" "$TAG" "$SOURCE_SHA" incomplete \
  >/dev/null

grep -q 'releases/17' "$DELETE_LOG"
if grep -q 'git/refs/tags' "$DELETE_LOG"; then
  echo "test-delete-preview: attempted to delete an absent tag" >&2
  exit 1
fi

source "$SCRIPT_DIR/lib.sh"
printf '%s' "$FAKE_MANIFEST" | base64 -d > "$TMP/daily-preview.yaml"
for arches in x86_64 'x86_64 aarch64'; do
  names=$(
    printf '%s\n' SHA256SUMS "$(platform_archive "$TAG")"
    while read -r unit version; do
      for arch in $arches; do component_archive "${unit%:}" "$version" "$arch"; done
    done < <(sed -n '/^components:/,$p' "$TMP/daily-preview.yaml" | tail -n +2)
  )
  assets=$(printf '%s\n' "$names" | jq -Rsc 'split("\n")[:-1]')
  log="$TMP/complete-${arches// /-}"
  if PATH="$TMP/bin:$PATH" GITHUB_REPOSITORY=kuasar-sandbox/kuasar-sandbox \
    FAKE_TAG="$TAG" FAKE_TARGET="$SOURCE_SHA" FAKE_COMPLETE_ASSETS="$assets" FAKE_DELETE_LOG="$log" FAKE_MANIFEST="$FAKE_MANIFEST" \
    bash "$SCRIPT_DIR/delete-preview.sh" "$TAG" "$SOURCE_SHA" incomplete > "$TMP/refusal" 2>&1; then
    echo 'test-delete-preview: complete aggregate was classified as incomplete' >&2; exit 1
  fi
  grep -Fq 'refusing incomplete recovery for a complete aggregate Preview' "$TMP/refusal"
  [ ! -e "$log" ]
done

echo "test-delete-preview: PASS"
