#!/usr/bin/env bash

set -euo pipefail

[ "$#" -eq 6 ] || {
  echo "usage: test-publisher.sh <publisher> <bundle> <repository> <tag> <commit> <source-ref>" >&2
  exit 2
}
PUBLISHER="$1"
BUNDLE="$2"
REPOSITORY="$3"
TAG="$4"
COMMIT="$5"
SOURCE_REF="$6"
EXPECTED_PRERELEASE=false
EXPECTED_LATEST=false
if [[ "$TAG" = *-preview.* ]]; then
  EXPECTED_PRERELEASE=true
  EXPECTED_LATEST=false
fi
if [ "$EXPECTED_PRERELEASE" = false ] && [ "$SOURCE_REF" = main ]; then
  EXPECTED_LATEST=true
fi
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/state"
# Unit-only registry call double. Real registry identity checks are covered by
# test_workbench_registry.py and require separate actual Preview acceptance.
real_python=$(command -v python3)
# Stage validation and registry transport are tested in Python; this fixture
# exercises GitHub recovery with the same human notes and accepted asset list.
cp -a "$BUNDLE" "$TMP/bundle"
BUNDLE="$TMP/bundle"
"$real_python" - "$BUNDLE" "$TMP/plan.json" <<'PY'
import hashlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
assets = [dict(name=p.name, size=p.stat().st_size, digest='sha256:'+hashlib.sha256(p.read_bytes()).hexdigest()) for p in (root/'assets').iterdir()]
pathlib.Path(sys.argv[2]).write_text(json.dumps({'baseline': {'assets': assets}}))
PY
cat > "$TMP/bin/python3" <<'PYTHON'
#!/bin/bash
set -euo pipefail
if [ "${1:-}" = -B ] && [[ "${2:-}" = */release/workbench_registry.py ]]; then
  printf '%s\n' "$*" >> "$FAKE_GH_STATE/registry-calls"
  exit 0
fi
exec "$REAL_TEST_PYTHON" "$@"
PYTHON
chmod +x "$TMP/bin/python3"
export REAL_TEST_PYTHON="$real_python"

cat > "$TMP/bin/gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

state="${FAKE_GH_STATE:?}"
repository="${FAKE_GH_REPOSITORY:?}"
tag="${FAKE_GH_TAG:?}"
commit="${FAKE_GH_COMMIT:?}"

not_found() {
  echo 'gh: Not Found (HTTP 404)' >&2
  exit 1
}

release_state() {
  local assets='[]' draft prerelease
  [ ! -s "$state/assets.ndjson" ] || assets="$(jq -s '.' "$state/assets.ndjson")"
  draft="$(cat "$state/release-draft")"
  prerelease="$(cat "$state/release-prerelease" 2>/dev/null || printf false)"
  commit="$(cat "$state/draft-target" 2>/dev/null || printf '%s' "$commit")"
  jq -cn --arg tag "$tag" --arg commit "$commit" --argjson draft "$draft" \
    --argjson prerelease "$prerelease" --argjson assets "$assets" --rawfile body "$state/notes" \
    '{id: 77, tag_name: $tag, target_commitish: $commit, draft: $draft,
      prerelease: $prerelease, assets: $assets, body: $body}'
}

emit() {
  local json="$1" filter="$2"
  if [ -n "$filter" ]; then jq -r "$filter" <<< "$json"; else printf '%s\n' "$json"; fi
}

if [ "${1:-}" = api ]; then
  shift
  method=GET
  input=
  filter=
  slurp=false
  endpoint=
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --method) method="$2"; shift 2 ;;
      --input) input="$2"; shift 2 ;;
      --jq) filter="$2"; shift 2 ;;
      --paginate) shift ;;
      --slurp) slurp=true; shift ;;
      -H) shift 2 ;;
      *) endpoint="$1"; shift ;;
    esac
  done
  request="$state/request.json"
  if [ "$input" = - ]; then
    cat > "$request"
  elif [ -n "$input" ]; then
    cp "$input" "$request"
  fi
  case "$method $endpoint" in
    "GET repos/$repository/git/ref/tags/$tag")
      [ -f "$state/tag" ] || not_found
      emit "$(jq -cn --arg sha "$(cat "$state/tag")" '{object: {sha: $sha}}')" "$filter"
      ;;
    "GET repos/$repository/commits/$tag")
      emit "$(jq -cn --arg sha "$(cat "$state/tag")" '{sha: $sha}')" "$filter"
      ;;
    "POST repos/$repository/git/refs")
      [ "$FAKE_GH_EXPECTED_PRERELEASE" = true ] || { echo 'Stable root tag created before public assets' >&2; exit 2; }
      [ "$(jq -er '.ref' "$request")" = "refs/tags/$tag" ] || exit 2
      jq -er '.sha' "$request" > "$state/tag"
      emit '{"ref":"created"}' "$filter"
      ;;
    "GET repos/$repository/releases/tags/$tag")
      [ -f "$state/release-draft" ] || not_found
      [ "$(cat "$state/release-draft")" = false ] || not_found
      emit "$(release_state)" "$filter"
      ;;
    "GET repos/$repository/releases?per_page=100")
      if [ -f "$state/release-draft" ] && [ "$(cat "$state/release-draft")" = true ]; then
        delay="$(cat "$state/visibility-delay" 2>/dev/null || printf 0)"
        if [ "$delay" -gt 0 ]; then
          printf '%s\n' "$((delay - 1))" > "$state/visibility-delay"
          json='[]'
        else
          json="[$(release_state)]"
        fi
      else
        json='[]'
      fi
      [ "$slurp" = false ] || json="[$json]"
      emit "$json" "$filter"
      ;;
    "DELETE repos/$repository/releases/77")
      rm -f "$state/release-draft" "$state/release-prerelease" \
        "$state/assets.ndjson" "$state/visibility-delay"
      count="$(cat "$state/delete-count" 2>/dev/null || printf 0)"
      printf '%s\n' "$((count + 1))" > "$state/delete-count"
      ;;
    "PATCH repos/$repository/releases/77")
      if jq -e 'has("body") and (has("draft") | not)' "$request" >/dev/null; then
        jq -j .body "$request" > "$state/notes"
        emit "$(release_state)" "$filter"
        exit 0
      fi
      [ "$(jq -er '.draft' "$request")" = false ] || exit 2
      if [ "$FAKE_GH_EXPECTED_PRERELEASE" = false ]; then
        expected="$(jq '.baseline.assets | length' "$FAKE_GH_PLAN")"
        actual="$(jq -s 'length' "$state/assets.ndjson")"
        [ "$actual" -eq "$expected" ] || { echo 'Stable tag would be published before complete assets' >&2; exit 2; }
        [ -f "$state/tag" ] || printf '%s\n' "$(cat "$state/draft-target")" > "$state/tag"
      fi
      jq -r '.prerelease' "$request" > "$state/release-prerelease"
      jq -er '.make_latest' "$request" > "$state/make-latest"
      printf 'false\n' > "$state/release-draft"
      emit "$(release_state)" "$filter"
      ;;
    *)
      echo "fake gh: unsupported API call: $method $endpoint" >&2
      exit 2
      ;;
  esac
  exit 0
fi

if [ "${1:-}" = release ] && [ "${2:-}" = upload ]; then
  [ "${3:-}" = "$tag" ] || exit 2
  shift 3
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --repo) shift 2 ;;
      --clobber) echo 'overwrite forbidden' >&2; exit 2 ;;
      *)
        file="$1"
        name=$(basename "$file")
        ! jq -es --arg name "$name" 'any(.[]; .name == $name)' "$state/assets.ndjson" >/dev/null || exit 2
        printf '%s\n' "$name" >> "$state/uploaded-on-retry"
        jq -cn --arg name "$name" --arg digest "sha256:$(sha256sum "$file" | awk '{print $1}')" \
          --argjson size "$(stat -c '%s' "$file")" \
          --argjson id "$(( $(wc -l < "$state/assets.ndjson") + 100 ))" \
          '{id:$id,name:$name,digest:$digest,size:$size,state:"uploaded"}' >> "$state/assets.ndjson"
        shift ;;
    esac
  done
  exit 0
fi

if [ "${1:-}" = release ] && [ "${2:-}" = create ]; then
  [ "${3:-}" = "$tag" ] || exit 2
  shift 3
  : > "$state/assets.ndjson"
  : > "$state/draft-target"
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --notes-file) cp "$2" "$state/notes"; shift 2 ;;
      --target) [ "$2" = "$commit" ] || exit 2; printf '%s\n' "$2" > "$state/draft-target"; shift 2 ;;
      --repo|--title) shift 2 ;;
      --draft) shift ;;
      --verify-tag) [ -f "$state/tag" ] || { echo 'verify-tag without tag' >&2; exit 2; }; shift ;;
      *)
        file="$1"
        jq -cn --arg name "$(basename "$file")" \
          --arg digest "sha256:$(sha256sum "$file" | awk '{print $1}')" \
          --argjson size "$(stat -c '%s' "$file")" \
          --argjson id "$(( $(wc -l < "$state/assets.ndjson") + 100 ))" \
          '{id:$id,name: $name, digest: $digest, size: $size, state: "uploaded"}' \
          >> "$state/assets.ndjson"
        shift
        ;;
    esac
  done
  [ -s "$state/draft-target" ] || { echo 'missing exact draft target' >&2; exit 2; }
  printf 'true\n' > "$state/release-draft"
  printf 'false\n' > "$state/release-prerelease"
  if [ "${FAKE_GH_FAIL_CREATE_ONCE:-0}" = 1 ] && [ ! -f "$state/failed-once" ]; then
    touch "$state/failed-once"
    exit 42
  fi
  printf '1\n' > "$state/visibility-delay"
  exit 0
fi

echo "fake gh: unsupported command" >&2
exit 2
EOF
chmod +x "$TMP/bin/gh"

common_env=(
  PATH="$TMP/bin:$PATH"
  GH_REPO="$REPOSITORY"
  RELEASE_PLAN_FILE="$TMP/plan.json"
  RELEASE_VALIDATION_FILE="$TMP/validation.json"
  RELEASE_WORKBENCH_RESULTS="$TMP/workbench"
  FAKE_GH_STATE="$TMP/state"
  FAKE_GH_REPOSITORY="$REPOSITORY"
  FAKE_GH_TAG="$TAG"
  FAKE_GH_COMMIT="$COMMIT"
  FAKE_GH_EXPECTED_PRERELEASE="$EXPECTED_PRERELEASE"
  FAKE_GH_PLAN="$TMP/plan.json"
)

env "${common_env[@]}" "$PUBLISHER" check "$TAG"
cp "$BUNDLE/release-notes.md" "$TMP/notes"
"$real_python" - "$BUNDLE/release-notes.md" <<'PY'
import json, pathlib, sys
notes = pathlib.Path(sys.argv[1])
notes.write_text('Human notes.\n' + 'x' * 125001)
PY
if env "${common_env[@]}" "$PUBLISHER" publish \
  "$TAG" "$COMMIT" "$BUNDLE" "$SOURCE_REF" > "$TMP/oversize.log" 2>&1; then
  echo 'test-publisher: accepted oversized human notes' >&2; exit 1
fi
grep -Fq 'notes must not be truncated' "$TMP/oversize.log"
for state in tag registry-calls release-draft; do
  [ ! -e "$TMP/state/$state" ] \
    || { echo "test-publisher: oversized body caused $state mutation" >&2; exit 1; }
done
mv "$TMP/notes" "$BUNDLE/release-notes.md"
if env "${common_env[@]}" FAKE_GH_FAIL_CREATE_ONCE=1 \
  "$PUBLISHER" publish "$TAG" "$COMMIT" "$BUNDLE" "$SOURCE_REF" >/dev/null 2>&1; then
  echo "test-publisher: interrupted draft creation unexpectedly succeeded" >&2
  exit 1
fi
[ "$(cat "$TMP/state/release-draft")" = true ] \
  || { echo "test-publisher: interrupted publish did not leave a draft" >&2; exit 1; }
if [ "$EXPECTED_PRERELEASE" = false ]; then
  [ ! -e "$TMP/state/tag" ] \
    || { echo "test-publisher: interrupted Stable publication exposed a root tag" >&2; exit 1; }
  cp "$TMP/state/draft-target" "$TMP/accepted-draft-target"
  printf '%040d\n' 0 > "$TMP/state/draft-target"
  if env "${common_env[@]}" "$PUBLISHER" publish \
    "$TAG" "$COMMIT" "$BUNDLE" "$SOURCE_REF" >"$TMP/mismatched-draft.log" 2>&1; then
    echo "test-publisher: resumed a Stable draft with the wrong target" >&2; exit 1
  fi
  grep -Fq 'existing Stable draft targets another commit' "$TMP/mismatched-draft.log"
  mv "$TMP/accepted-draft-target" "$TMP/state/draft-target"
fi
cp "$TMP/state/assets.ndjson" "$TMP/original-assets"
registry_calls=$(grep -vc -- --validate-only "$TMP/state/registry-calls")
jq -sc '.[0].digest = "sha256:wrong" | .[]' "$TMP/original-assets" > "$TMP/state/assets.ndjson"
if env "${common_env[@]}" "$PUBLISHER" publish \
  "$TAG" "$COMMIT" "$BUNDLE" "$SOURCE_REF" > "$TMP/conflict.log" 2>&1; then
  echo 'test-publisher: accepted conflicting existing asset bytes' >&2; exit 1
fi
grep -Fq 'existing draft asset differs' "$TMP/conflict.log"
[ "$(grep -vc -- --validate-only "$TMP/state/registry-calls")" = "$registry_calls" ]
[ "$(cat "$TMP/state/release-draft")" = true ]
if [ "$EXPECTED_PRERELEASE" = false ]; then
  [ ! -e "$TMP/state/tag" ] \
    || { echo "test-publisher: rejected Stable asset mismatch exposed a root tag" >&2; exit 1; }
fi
missing=$(tail -n 1 "$TMP/original-assets" | jq -r .name)
head -n -1 "$TMP/original-assets" > "$TMP/state/assets.ndjson"
env "${common_env[@]}" "$PUBLISHER" publish \
  "$TAG" "$COMMIT" "$BUNDLE" "$SOURCE_REF"
[ "$(cat "$TMP/state/uploaded-on-retry")" = "$missing" ]
[ ! -e "$TMP/state/delete-count" ] \
  || { echo "test-publisher: retry replaced an existing draft instead of verifying its bytes" >&2; exit 1; }
[ "$(cat "$TMP/state/tag")" = "$COMMIT" ] \
  || { echo "test-publisher: tag points to the wrong commit" >&2; exit 1; }
[ "$(cat "$TMP/state/release-draft")" = false ] \
  || { echo "test-publisher: release remains a draft" >&2; exit 1; }
[ "$(cat "$TMP/state/release-prerelease")" = "$EXPECTED_PRERELEASE" ] \
  || { echo "test-publisher: release has the wrong prerelease state" >&2; exit 1; }
[ "$(cat "$TMP/state/make-latest")" = "$EXPECTED_LATEST" ] \
  || { echo "test-publisher: release has the wrong latest policy" >&2; exit 1; }
if env "${common_env[@]}" "$PUBLISHER" check "$TAG" >/dev/null 2>&1; then
  echo "test-publisher: preflight accepted an already published release" >&2
  exit 1
fi

# A finalization response can be lost after the Release became public.
# The original successful stage must converge by verification, without edits.
cp "$TMP/state/assets.ndjson" "$TMP/public-assets"
env "${common_env[@]}" "$PUBLISHER" publish "$TAG" "$COMMIT" "$BUNDLE" "$SOURCE_REF"
cmp "$TMP/public-assets" "$TMP/state/assets.ndjson"
tail -n 1 "$TMP/state/registry-calls" | grep -Fq -- --read-only
printf 'changed human notes' > "$TMP/state/notes"
if env "${common_env[@]}" "$PUBLISHER" publish "$TAG" "$COMMIT" "$BUNDLE" "$SOURCE_REF" > "$TMP/public-conflict.log" 2>&1; then
  echo 'test-publisher: accepted a different published body' >&2; exit 1
fi
grep -Fq 'published Release differs from the original stage' "$TMP/public-conflict.log"

echo "test-publisher: PASS"
