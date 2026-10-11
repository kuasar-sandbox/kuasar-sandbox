#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"
cat > "$TMP/bin/gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${2:-}" == repos/kuasar-sandbox/kuasar-sandbox/git/ref/heads/* ]]; then
  if [[ -n "${FAKE_READ_MARKER:-}" && -f "$FAKE_READ_MARKER" ]]; then
    printf '%s\n' "${FAKE_AFTER_READ_SHA:-1111111111111111111111111111111111111111}"
  else
    printf '%s\n' "${FAKE_BRANCH_SHA:-1111111111111111111111111111111111111111}"
  fi
  exit 0
fi
if [[ "${2:-}" == repos/kuasar-sandbox/kuasar-sandbox/contents/* ]]; then
  [[ "$2" == *'?ref=main' || "$2" == *'?ref=release/v'* ]] || { echo 'raw SHA source lookup rejected' >&2; exit 1; }
  if [[ -n "${FAKE_READ_MARKER:-}" ]]; then touch "$FAKE_READ_MARKER"; fi
  printf '%s\n' "${FAKE_MANIFEST:?}"
  exit 0
fi
if [ "${FAKE_STABLE_EXISTS:-0}" = 1 ]; then
  printf '{}\n'
  exit 0
fi
echo 'gh: Not Found (HTTP 404)' >&2
exit 1
EOF
chmod +x "$TMP/bin/gh"

TAG=release-v9.8.7-preview.20260831
SOURCE_SHA=1111111111111111111111111111111111111111
MANIFEST="$(printf '%s\n' \
  'version: release-v9.8.7' \
  'preview_version: preview.20260831' \
  'components:' \
  '    version: ignored-nested-value' | base64 -w0)"

PATH="$TMP/bin:$PATH" FAKE_MANIFEST="$MANIFEST" \
  bash "$SCRIPT_DIR/validate-preview-line.sh" "$TAG" "$SOURCE_SHA"
PATH="$TMP/bin:$PATH" bash "$SCRIPT_DIR/validate-preview-line.sh" \
  release-v9.8.7 ""

if PATH="$TMP/bin:$PATH" FAKE_MANIFEST="$MANIFEST" FAKE_STABLE_EXISTS=1 \
  bash "$SCRIPT_DIR/validate-preview-line.sh" "$TAG" "$SOURCE_SHA" \
  >/dev/null 2>&1; then
  echo "test-preview-line: accepted a Preview for a closed Stable line" >&2
  exit 1
fi

BAD_MANIFEST="$(printf '%s\n' \
  'version: release-v9.8.8' \
  'preview_version: preview.20260831' \
  'components:' | base64 -w0)"
if PATH="$TMP/bin:$PATH" FAKE_MANIFEST="$BAD_MANIFEST" \
  bash "$SCRIPT_DIR/validate-preview-line.sh" "$TAG" "$SOURCE_SHA" \
  >/dev/null 2>&1; then
  echo "test-preview-line: accepted a fabricated aggregate Preview" >&2
  exit 1
fi

REVISION_MANIFEST="$(printf '%s' "$MANIFEST" | base64 -d \
  | sed 's/preview.20260831/preview.20260831.1/g' | base64 -w0)"
PATH="$TMP/bin:$PATH" FAKE_MANIFEST="$REVISION_MANIFEST" \
  bash "$SCRIPT_DIR/validate-preview-line.sh" "$TAG.1" "$SOURCE_SHA"
if PATH="$TMP/bin:$PATH" FAKE_MANIFEST="$REVISION_MANIFEST" \
  bash "$SCRIPT_DIR/validate-preview-line.sh" "$TAG.2" "$SOURCE_SHA" \
  >/dev/null 2>&1; then
  echo "test-preview-line: accepted another revision than the manifest" >&2
  exit 1
fi

if PATH="$TMP/bin:$PATH" FAKE_MANIFEST="$MANIFEST" FAKE_BRANCH_SHA=2222222222222222222222222222222222222222 \
  bash "$SCRIPT_DIR/validate-preview-line.sh" "$TAG" "$SOURCE_SHA" > "$TMP/moved-before.log" 2>&1; then
  echo 'test-preview-line: accepted unmatched aggregate identity' >&2; exit 1
fi
grep -Fq 'not an admitted branch HEAD' "$TMP/moved-before.log"
if PATH="$TMP/bin:$PATH" FAKE_MANIFEST="$MANIFEST" FAKE_READ_MARKER="$TMP/read-marker" \
  FAKE_AFTER_READ_SHA=2222222222222222222222222222222222222222 \
  bash "$SCRIPT_DIR/validate-preview-line.sh" "$TAG" "$SOURCE_SHA" > "$TMP/moved-after.log" 2>&1; then
  echo 'test-preview-line: accepted movement during manifest read' >&2; exit 1
fi
grep -Fq 'moved during manifest admission' "$TMP/moved-after.log"
echo "test-preview-line: PASS"
