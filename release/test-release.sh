#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=release/lib.sh
source "$ROOT/release/lib.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

VERSION=release-v0.1.0-preview.20260809
resolve_selection "$ROOT" "$VERSION" "$TMP/selection.tsv"
[ "$(previous_release "$ROOT" "$VERSION")" = release-v0.1.0-preview.20260808 ] \
  || release_fail "release manifest did not resolve its explicit previous version"
mkdir -p "$TMP/fetched/components" "$TMP/fetched/updates"
install -m 0644 "$TMP/selection.tsv" "$TMP/fetched/selection.tsv"
resolve_selection "$ROOT" "$(previous_release "$ROOT" "$VERSION")" \
  "$TMP/fetched/previous-selection.tsv"

while IFS=$'\t' read -r unit tag; do
  archive="$(component_archive "$unit" "$tag")"
  stage="$TMP/stage-$unit"
  directory="$TMP/fetched/components/$unit"
  mkdir -p "$stage/bin" "$directory"
  printf '%s %s\n' "$unit" "$tag" > "$stage/bin/$unit"
  chmod +x "$stage/bin/$unit"
  tar --sort=name --owner=0 --group=0 --numeric-owner --mtime='@1700000000' \
    -czf "$directory/$archive" -C "$stage" .
  (cd "$directory" && sha256sum "$archive" > SHA256SUMS)
  printf "Fixture updates for \`%s\`.\n" "$unit" > "$TMP/fetched/updates/$unit.md"
done < "$TMP/selection.tsv"

SOURCE_DATE_EPOCH=1700000000 \
  "$ROOT/release/aggregate-release.sh" assemble "$VERSION" "$TMP/fetched" "$TMP/bundle"
"$ROOT/release/aggregate-release.sh" validate "$VERSION" "$TMP/bundle"
"$ROOT/release/test-publisher.sh" "$ROOT/release/publish-release.sh" \
  "$TMP/bundle" kuasar-sandbox/platform "$VERSION" \
  1111111111111111111111111111111111111111

[ "$(find "$TMP/bundle/assets" -maxdepth 1 -type f | wc -l)" -eq 8 ] \
  || release_fail "aggregate bundle must contain exactly eight assets"
[ ! -e "$TMP/bundle/release.json" ] || release_fail "aggregate bundle contains release.json"
if find "$TMP/bundle/assets" -maxdepth 1 -type f -name 'release-*.yaml' | grep -q .; then
  release_fail "aggregate assets contain a release manifest"
fi
for unit in "${RELEASE_UNITS[@]}"; do
  grep -Fqx "### $unit" "$TMP/bundle/release-notes.md" \
    || release_fail "aggregate release notes omit $unit updates"
done
if tar -tzf "$TMP/bundle/assets/$(platform_archive "$VERSION")" \
  | grep -E '(^|/)release\.json$|(^|/)release/[^/]+\.json$' >/dev/null; then
  release_fail "platform package contains release metadata JSON"
fi
if tar -tzf "$TMP/bundle/assets/$(platform_archive "$VERSION")" \
  | grep -E '(^|/)releases/release-v[^/]+\.yaml$' >/dev/null; then
  release_fail "platform package contains a release manifest"
fi

"$ROOT/release/aggregate-release.sh" extract "$VERSION" "$TMP/bundle" "$TMP/install"
[ -f "$TMP/install/docs/kuasar-sandbox.md" ] || release_fail "platform docs were not extracted"
[ -x "$TMP/install/test/e2e/run_all.sh" ] || release_fail "platform E2E runner was not extracted"

runner_root="$TMP/runner-root"
mkdir -p "$runner_root/bin" "$runner_root/test/e2e"
install -m 0755 "$TMP/install/test/e2e/run_all.sh" "$runner_root/test/e2e/run_all.sh"
cat > "$runner_root/test/e2e/e2e_binary_paths.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[ "$FLATTEN_CTL" = "$BIN/flatten-ctl" ]
[ "$STORE_CTL" = "$BIN/store-ctl" ]
[ "$MKFS_EROFS_PATH" = "$BIN/mkfs.erofs" ]
EOF
chmod +x "$runner_root/test/e2e/e2e_binary_paths.sh"
BIN="$runner_root/bin" bash "$runner_root/test/e2e/run_all.sh" > "$TMP/runner.out"
grep -Fq '==> full release e2e: OK' "$TMP/runner.out" \
  || release_fail "platform E2E runner did not complete its binary-path check"

for unit in "${RELEASE_UNITS[@]}"; do
  [ -x "$TMP/install/bin/$unit" ] || release_fail "$unit fixture was not extracted"
done

cp -a "$TMP/bundle" "$TMP/tampered"
printf 'tampered\n' >> "$TMP/tampered/assets/$(platform_archive "$VERSION")"
if "$ROOT/release/aggregate-release.sh" validate "$VERSION" "$TMP/tampered" >/dev/null 2>&1; then
  release_fail "aggregate validator accepted a tampered platform package"
fi

if "$ROOT/release/selection.py" "$ROOT" release-v1.2.3 >/dev/null 2>&1; then
  release_fail "selection resolver accepted a missing release manifest"
fi
if "$ROOT/release/selection.py" "$ROOT" release-v1.2.3-preview.20260808 >/dev/null 2>&1; then
  release_fail "selection resolver derived a preview without a release manifest"
fi

mkdir -p "$TMP/coordinator-bin" "$TMP/coordinator-state"
cat > "$TMP/coordinator-bin/gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

state="${FAKE_COORDINATOR_STATE:?}"
if [ "${1:-}" = api ]; then
  shift
  endpoint=
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --paginate|--slurp) shift ;;
      *) endpoint="$1"; shift ;;
    esac
  done
  if [ "${FAKE_RELEASE_API_ERROR:-0}" = 1 ] \
    && [[ "$endpoint" == repos/*/releases/tags/* ]]; then
    echo 'Get "https://api.github.com/release": unexpected EOF' >&2
    exit 1
  fi
  case "$endpoint" in
    repos/*/releases/tags/*)
      echo 'gh: Not Found (HTTP 404)' >&2
      exit 1
      ;;
    repos/*/actions/workflows/*/runs\?*)
      printf '[{"workflow_runs":[]}]\n'
      exit 0
      ;;
  esac
  echo "fake coordinator gh: unsupported API call: $endpoint" >&2
  exit 2
fi

if [ "${1:-}" = workflow ] && [ "${2:-}" = run ]; then
  printf '%s\n' "$*" >> "$state/dispatches"
  exit 0
fi

echo "fake coordinator gh: unsupported command: $*" >&2
exit 2
EOF
chmod +x "$TMP/coordinator-bin/gh"

mkdir -p "$TMP/coordinator-error-state"
if PATH="$TMP/coordinator-bin:$PATH" \
  FAKE_COORDINATOR_STATE="$TMP/coordinator-error-state" \
  FAKE_RELEASE_API_ERROR=1 PREVIEW_POLL_SECONDS=0 PREVIEW_WAIT_SECONDS=0 \
  bash "$ROOT/release/preview-coordinator.sh" --version "$VERSION" \
  > "$TMP/coordinator-error.out" 2>&1; then
  release_fail "preview coordinator treated a release API error as a missing release"
fi
[ ! -e "$TMP/coordinator-error-state/dispatches" ] \
  || release_fail "preview coordinator dispatched a workflow after a release API error"

PATH="$TMP/coordinator-bin:$PATH" \
  FAKE_COORDINATOR_STATE="$TMP/coordinator-state" \
  PREVIEW_POLL_SECONDS=0 PREVIEW_WAIT_SECONDS=0 \
  bash "$ROOT/release/preview-coordinator.sh" 20260809 > "$TMP/coordinator.out"
[ "$(wc -l < "$TMP/coordinator-state/dispatches")" -eq 5 ] \
  || release_fail "preview coordinator did not dispatch the five independent release units"
grep -Fqx 'workflow run release.yml --repo kuasar-sandbox/accelerator --ref main -f version=v0.1.0-preview.20260809' \
  "$TMP/coordinator-state/dispatches" \
  || release_fail "preview coordinator did not dispatch the expected accelerator release"
grep -Fq 'preview remains pending' "$TMP/coordinator.out" \
  || release_fail "preview coordinator did not preserve pending convergence"

mkdir -p "$TMP/generated-root" "$TMP/generated-bin" "$TMP/generated-state"
cp -a "$ROOT/release" "$ROOT/releases" "$TMP/generated-root/"
cat > "$TMP/generated-bin/gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

state="${FAKE_COORDINATOR_STATE:?}"

archive_name() {
  local repository="$1" tag="$2" component="${repository##*/}"
  case "$component:$tag" in
    guest-runtime:runtime-*) printf 'sandbox-runtime-x86_64-%s.tar.gz\n' "${tag#runtime-}" ;;
    guest-runtime:vmlinux-*) printf 'vmlinux-x86_64-%s.tar.gz\n' "${tag#vmlinux-}" ;;
    *) printf '%s-%s-linux-x86_64.tar.gz\n' "$component" "$tag" ;;
  esac
}

component_release() {
  local repository="$1" tag="$2" archive
  archive="$(archive_name "$repository" "$tag")"
  jq -n --arg tag "$tag" --arg archive "$archive" '
    {tag_name: $tag, draft: false, prerelease: true,
     published_at: "2026-08-09T00:00:00Z",
     assets: [{name: $archive}, {name: "SHA256SUMS"}]}'
}

if [ "${1:-}" = api ]; then
  shift
  method=GET
  endpoint=
  input=
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --paginate|--slurp) shift ;;
      --method) method="$2"; shift 2 ;;
      --input) input="$2"; shift 2 ;;
      --jq) shift 2 ;;
      -*) shift ;;
      *) endpoint="$1"; shift ;;
    esac
  done

  if [ "$method" = PUT ] && [[ "$endpoint" == repos/kuasar-sandbox/platform/contents/releases/* ]]; then
    cp "$input" "$state/manifest-request.json"
    printf '%s\n' "$endpoint" > "$state/manifest-endpoint"
    printf '{"commit":{"sha":"2222222222222222222222222222222222222222"}}\n'
    exit 0
  fi

  case "$endpoint" in
    repos/kuasar-sandbox/platform/releases\?*)
      printf '[[{"tag_name":"release-v0.1.0-preview.20260809","draft":false,"published_at":"2026-08-09T06:00:00Z"}]]\n'
      ;;
    repos/*/releases\?*)
      repository="${endpoint#repos/}"
      repository="${repository%%/releases*}"
      if [ "$repository" = kuasar-sandbox/guest-runtime ]; then
        printf '[[%s,%s]]\n' \
          "$(component_release "$repository" runtime-v0.1.0-preview.20260809)" \
          "$(component_release "$repository" vmlinux-v0.1.0-preview.20260809)"
      else
        printf '[[%s]]\n' \
          "$(component_release "$repository" v0.1.0-preview.20260809)"
      fi
      ;;
    repos/*/releases/tags/*)
      repository="${endpoint#repos/}"
      repository="${repository%%/releases/tags/*}"
      tag="${endpoint##*/}"
      if [[ "$tag" == *preview.20260809 ]]; then
        component_release "$repository" "$tag"
      else
        echo 'gh: Not Found (HTTP 404)' >&2
        exit 1
      fi
      ;;
    repos/*/actions/workflows/*/runs\?*)
      printf '[{"workflow_runs":[]}]\n'
      ;;
    *)
      echo "fake generated coordinator gh: unsupported API call: $endpoint" >&2
      exit 2
      ;;
  esac
  exit 0
fi

if [ "${1:-}" = workflow ] && [ "${2:-}" = run ]; then
  printf '%s\n' "$*" >> "$state/dispatches"
  exit 0
fi

echo "fake generated coordinator gh: unsupported command: $*" >&2
exit 2
EOF
chmod +x "$TMP/generated-bin/gh"

cat > "$TMP/generated-bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

url="${!#}"
case "$url" in
  */repos/kuasar-sandbox/*/commits/main)
    repository="${url#*/repos/}"
    repository="${repository%/commits/main}"
    jq -n --arg sha "head-${repository##*/}" '{sha: $sha}'
    ;;
  */repos/kuasar-sandbox/*/compare/*)
    if [[ "$url" == */repos/kuasar-sandbox/accelerator/compare/* ]]; then
      printf '{"status":"ahead"}\n'
    else
      printf '{"status":"identical"}\n'
    fi
    ;;
  *)
    echo "fake generated coordinator curl: unsupported URL: $url" >&2
    exit 2
    ;;
esac
EOF
chmod +x "$TMP/generated-bin/curl"

PATH="$TMP/generated-bin:$PATH" \
  FAKE_COORDINATOR_STATE="$TMP/generated-state" \
  GH_TOKEN=read-token PLATFORM_TOKEN=write-token \
  PREVIEW_POLL_SECONDS=0 PREVIEW_WAIT_SECONDS=0 \
  bash "$TMP/generated-root/release/preview-coordinator.sh" 20260810 \
  > "$TMP/generated-coordinator.out"

GENERATED_VERSION=release-v0.1.0-preview.20260810
GENERATED_MANIFEST="$TMP/generated-root/releases/$GENERATED_VERSION.yaml"
[ -f "$GENERATED_MANIFEST" ] || release_fail "daily coordinator did not generate a manifest"
[ "$(previous_release "$TMP/generated-root" "$GENERATED_VERSION")" = "$VERSION" ] \
  || release_fail "generated manifest does not name the latest aggregate as previous"
resolve_selection "$TMP/generated-root" "$GENERATED_VERSION" "$TMP/generated-selection.tsv"
grep -Fqx $'accelerator\tv0.1.0-preview.20260810' "$TMP/generated-selection.tsv" \
  || release_fail "changed component did not select a new preview"
for unit in connector sandboxer orchestrator; do
  grep -Fqx "$unit"$'\t''v0.1.0-preview.20260809' "$TMP/generated-selection.tsv" \
    || release_fail "$unit did not reuse its unchanged release"
done
grep -Fqx $'runtime\truntime-v0.1.0-preview.20260809' "$TMP/generated-selection.tsv" \
  || release_fail "runtime did not reuse its unchanged release"
grep -Fqx $'vmlinux\tvmlinux-v0.1.0-preview.20260809' "$TMP/generated-selection.tsv" \
  || release_fail "vmlinux did not reuse its unchanged release"
[ "$(cat "$TMP/generated-state/manifest-endpoint")" = \
  "repos/kuasar-sandbox/platform/contents/releases/$GENERATED_VERSION.yaml" ] \
  || release_fail "generated manifest was committed to an unexpected path"
jq -r .content "$TMP/generated-state/manifest-request.json" | base64 -d \
  > "$TMP/persisted-manifest"
cmp -s "$GENERATED_MANIFEST" "$TMP/persisted-manifest" \
  || release_fail "committed manifest differs from the frozen local selection"
[ "$(wc -l < "$TMP/generated-state/dispatches")" -eq 1 ] \
  || release_fail "daily coordinator dispatched an unchanged release unit"
grep -Fqx 'workflow run release.yml --repo kuasar-sandbox/accelerator --ref main -f version=v0.1.0-preview.20260810' \
  "$TMP/generated-state/dispatches" \
  || release_fail "daily coordinator did not dispatch the changed release unit"

echo "test-release: PASS"
