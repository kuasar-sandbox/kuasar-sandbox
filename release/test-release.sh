#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=release/lib.sh
source "$ROOT/release/lib.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PREVIEW_BASE="$(awk '$1 == "version:" {print $2}' "$ROOT/releases/daily-preview.yaml")"
PREVIEW_SUFFIX="$(awk '$1 == "preview_version:" {print $2}' "$ROOT/releases/daily-preview.yaml")"
PREVIEW_DATE="${PREVIEW_SUFFIX#preview.}"
NEXT_PREVIEW_DATE="$(date -u -d "$PREVIEW_DATE + 1 day" +%Y%m%d)"
PREVIEW_VERSION="$PREVIEW_BASE-$PREVIEW_SUFFIX"
PREVIEW_COMPONENT_VERSION="${PREVIEW_BASE#release-}"
PREVIEW_WORKFLOW="$ROOT/.github/workflows/daily-preview.yml"
WORKFLOW_WAIT_SECONDS="$(awk -F '"' '/PREVIEW_WAIT_SECONDS:/ {print $2}' "$PREVIEW_WORKFLOW")"
WORKFLOW_TIMEOUT_MINUTES="$(awk '/timeout-minutes:/ {print $2; exit}' "$PREVIEW_WORKFLOW")"
[[ "$WORKFLOW_WAIT_SECONDS" =~ ^[0-9]+$ ]]
[[ "$WORKFLOW_TIMEOUT_MINUTES" =~ ^[0-9]+$ ]]
[ "$WORKFLOW_WAIT_SECONDS" -lt 3600 ] \
  || release_fail "daily preview wait exceeds the GitHub App token lifetime"
[ "$WORKFLOW_WAIT_SECONDS" -lt "$((WORKFLOW_TIMEOUT_MINUTES * 60))" ] \
  || release_fail "daily preview wait must fit within the coordinator job timeout"
COORDINATOR_INFRA_RE="$(sed -n \
  "s/^readonly INFRA_FAILURE_RE='\\(.*\\)'$/\\1/p" \
  "$ROOT/release/preview-coordinator.sh")"
[ -n "$COORDINATOR_INFRA_RE" ] \
  || release_fail "daily preview infrastructure failure classifier is missing"
printf '%s\n' 'e2e: timed out waiting for zot (127.0.0.1:37806)' \
  | grep -Eiq "$COORDINATOR_INFRA_RE" \
  || release_fail "daily preview does not retry a zot startup timeout"
printf '%s\n' 'e2e: zot failed to start after 2 attempts' \
  | grep -Eiq "$COORDINATOR_INFRA_RE" \
  || release_fail "daily preview does not retry an exhausted zot startup"
if printf '%s\n' 'E2E FAILED: product assertion mismatch' \
  | grep -Eiq "$COORDINATOR_INFRA_RE"; then
  release_fail "daily preview retries an ordinary product test failure"
fi
if printf '%s\n' 'zot startup attempt timed out on 127.0.0.1:37806' \
  | grep -Eiq "$COORDINATOR_INFRA_RE"; then
  release_fail "daily preview treats a recovered zot attempt as a workflow failure"
fi
resolve_selection "$ROOT" "$PREVIEW_VERSION" "$TMP/current-preview-selection.tsv"
PREVIEW_ACCELERATOR_TAG="$(awk -F '\t' '$1 == "accelerator" {print $2}' \
  "$TMP/current-preview-selection.tsv")"

write_preview_base_fixture() {
  local output="$1" unit tag
  {
    printf 'version: %s\ncomponents:\n' "$PREVIEW_BASE"
    while IFS=$'\t' read -r unit tag; do
      printf '  %s: %s\n' "$unit" "${tag%%-preview.*}"
    done < "$TMP/current-preview-selection.tsv"
  } > "$output"
}

CURRENT_FORMAL_VERSION="$(awk '$1 == "version:" {print $2}' \
  "$ROOT/releases/release.yaml")"
resolve_selection "$ROOT" "$CURRENT_FORMAL_VERSION" \
  "$TMP/current-formal-selection.tsv"

FORMAL_ROOT="$TMP/formal-root"
mkdir -p "$FORMAL_ROOT"
tar -C "$ROOT" --exclude='./.git' -cf - . | tar -x -C "$FORMAL_ROOT"
write_preview_base_fixture "$FORMAL_ROOT/releases/release.yaml"
git -C "$FORMAL_ROOT" init -q
git -C "$FORMAL_ROOT" config user.name release-test
git -C "$FORMAL_ROOT" config user.email release-test@example.invalid
git -C "$FORMAL_ROOT" add .
git -C "$FORMAL_ROOT" commit -qm 'maintain first formal release fixture'

VERSION="$PREVIEW_BASE"
resolve_selection "$FORMAL_ROOT" "$VERSION" "$TMP/selection.tsv"
[ -z "$(previous_release "$FORMAL_ROOT" "$VERSION")" ] \
  || release_fail "first formal release unexpectedly has a comparison baseline"
mkdir -p "$TMP/fetched/components" "$TMP/fetched/sources" "$TMP/fetched/updates"
install -m 0644 "$TMP/selection.tsv" "$TMP/fetched/selection.tsv"
: > "$TMP/fetched/previous-selection.tsv"

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

  source_root="$TMP/fetched/sources/$unit"
  mkdir -p "$source_root/docs" "$source_root/test/e2e"
  printf '# %s source fixture\n' "$unit" > "$source_root/README.md"
  printf '%s docs\n' "$unit" > "$source_root/docs/$unit-detail.md"
  cat > "$source_root/test/e2e/run_all.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
echo "$unit fixture E2E"
EOF
  chmod +x "$source_root/test/e2e/run_all.sh"
done < "$TMP/selection.tsv"
printf 'runtime copy of vmlinux docs\n' > "$TMP/fetched/sources/runtime/docs/vmlinux.md"
printf 'selected vmlinux docs\n' > "$TMP/fetched/sources/vmlinux/docs/vmlinux.md"

SOURCE_DATE_EPOCH=1700000000 \
  "$FORMAL_ROOT/release/aggregate-release.sh" assemble "$VERSION" "$TMP/fetched" "$TMP/bundle"
"$FORMAL_ROOT/release/aggregate-release.sh" validate "$VERSION" "$TMP/bundle"
"$FORMAL_ROOT/release/test-publisher.sh" "$FORMAL_ROOT/release/publish-release.sh" \
  "$TMP/bundle" kuasar-sandbox/kuasar-sandbox "$VERSION" \
  1111111111111111111111111111111111111111

[ "$(find "$TMP/bundle/assets" -maxdepth 1 -type f | wc -l)" -eq 8 ] \
  || release_fail "aggregate bundle must contain exactly eight assets"
[ ! -e "$TMP/bundle/release.json" ] || release_fail "aggregate bundle contains release.json"
if find "$TMP/bundle/assets" -maxdepth 1 -type f -name 'release-*.yaml' | grep -q .; then
  release_fail "aggregate assets contain a release manifest"
fi
for unit in "${RELEASE_UNITS[@]}"; do
  tag="$(awk -F '\t' -v unit="$unit" '$1 == unit {print $2}' "$TMP/selection.tsv")"
  grep -Fqx "| \`$unit\` | \`$tag\` |" "$TMP/bundle/release-notes.md" \
    || release_fail "aggregate release notes omit $unit from the version table"
  grep -Fqx "### $unit" "$TMP/bundle/release-notes.md" \
    || release_fail "aggregate release notes omit $unit updates"
done
for heading in \
  '## Highlights' \
  '## Supported environment' \
  '## Quick Start' \
  '## Production deployment' \
  '## Known limitations' \
  '## Security' \
  '## Versioning and release channels'; do
  grep -Fqx "$heading" "$TMP/bundle/release-notes.md" \
    || release_fail "aggregate release notes omit $heading"
done
grep -Fq 'This aggregate is a Stable, non-prerelease release.' \
  "$TMP/bundle/release-notes.md" \
  || release_fail "formal release notes do not identify the Stable channel"
grep -Fq "https://github.com/kuasar-sandbox/kuasar-sandbox/blob/$VERSION/docs/quickstart.md" \
  "$TMP/bundle/release-notes.md" \
  || release_fail "formal release notes omit the Quick Start link"
grep -Fq 'https://github.com/kuasar-sandbox/kuasar-sandbox/security/advisories/new' \
  "$TMP/bundle/release-notes.md" \
  || release_fail "formal release notes omit the private security reporting link"
grep -Fq 'This is the first formal aggregate release' "$TMP/bundle/release-notes.md" \
  || release_fail "first formal release notes do not identify the missing baseline"
if grep -Fq 'Previous aggregate selection:' "$TMP/bundle/release-notes.md"; then
  release_fail "first formal release notes contain a preview comparison baseline"
fi
if tar -tzf "$TMP/bundle/assets/$(platform_archive "$VERSION")" \
  | grep -E '(^|/)release\.json$|(^|/)release/[^/]+\.json$' >/dev/null; then
  release_fail "platform package contains release metadata JSON"
fi
if tar -tzf "$TMP/bundle/assets/$(platform_archive "$VERSION")" \
  | grep -E '(^|/)releases/[^/]+\.yaml$' >/dev/null; then
  release_fail "platform package contains a release manifest"
fi
if tar -tzf "$TMP/bundle/assets/$(platform_archive "$VERSION")" \
  | grep -F './test/e2e/assemble.sh' >/dev/null; then
  release_fail "platform package contains the source-only E2E assembler"
fi

"$FORMAL_ROOT/release/aggregate-release.sh" extract "$VERSION" "$TMP/bundle" "$TMP/install"
[ -f "$TMP/install/docs/kuasar-sandbox.md" ] || release_fail "platform docs were not extracted"
[ -x "$TMP/install/test/e2e/run_all.sh" ] || release_fail "platform E2E runner was not extracted"
for owner in accelerator connector guest-runtime sandboxer orchestrator platform; do
  [ -x "$TMP/install/test/e2e/$owner/run_all.sh" ] \
    || release_fail "$owner E2E runner was not aggregated"
done
for component in accelerator connector guest-runtime sandboxer orchestrator; do
  [ -f "$TMP/install/docs/$component.md" ] \
    || release_fail "$component README was not aggregated"
done
grep -Fqx 'selected vmlinux docs' "$TMP/install/docs/vmlinux.md" \
  || release_fail "vmlinux docs did not come from the selected vmlinux source"

runner_root="$TMP/runner-root"
mkdir -p "$runner_root/bin" "$runner_root/test/e2e"
install -m 0755 "$TMP/install/test/e2e/run_all.sh" "$runner_root/test/e2e/run_all.sh"
for owner in accelerator connector guest-runtime sandboxer orchestrator platform; do
  mkdir -p "$runner_root/test/e2e/$owner"
  cat > "$runner_root/test/e2e/$owner/run_all.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
[ "\$BIN" = "$runner_root/bin" ]
echo "$owner owner runner"
EOF
  chmod +x "$runner_root/test/e2e/$owner/run_all.sh"
done
BIN="$runner_root/bin" ZOT_BIN=/bin/true VGW_BIN=/bin/true \
  bash "$runner_root/test/e2e/run_all.sh" > "$TMP/runner.out"
grep -Fq '==> full release e2e: OK' "$TMP/runner.out" \
  || release_fail "platform E2E runner did not complete its owner-runner check"

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

history_root="$TMP/selection-history"
mkdir -p "$history_root/release" "$history_root/releases"
install -m 0755 "$ROOT/release/selection.py" "$history_root/release/selection.py"
cat > "$history_root/releases/release.yaml" <<'EOF'
version: release-v0.1.0
components:
  accelerator: v0.1.0
  connector: v0.1.0
  sandboxer: v0.1.0
  orchestrator: v0.1.0
  runtime: runtime-v0.1.0
  vmlinux: vmlinux-v0.1.0
EOF
cat > "$history_root/releases/daily-preview.yaml" <<'EOF'
version: release-v0.1.0
preview_version: preview.20260808
components:
  accelerator: v0.1.0-preview.20260808
  connector: v0.1.0-preview.20260808
  sandboxer: v0.1.0-preview.20260808
  orchestrator: v0.1.0-preview.20260808
  runtime: runtime-v0.1.0-preview.20260808
  vmlinux: vmlinux-v0.1.0-preview.20260808
EOF
git -C "$history_root" init -q
git -C "$history_root" config user.name release-test
git -C "$history_root" config user.email release-test@example.invalid
git -C "$history_root" add release/selection.py releases/release.yaml releases/daily-preview.yaml
git -C "$history_root" commit -qm 'initial release state'

cat > "$history_root/releases/release.yaml" <<'EOF'
version: release-v0.2.0
previous_version: release-v0.1.0
components:
  accelerator: v0.2.0
  connector: v0.2.0
  sandboxer: v0.2.0
  orchestrator: v0.2.0
  runtime: runtime-v0.2.0
  vmlinux: vmlinux-v0.2.0
EOF
cat > "$history_root/releases/daily-preview.yaml" <<'EOF'
version: release-v0.2.0
previous_version: release-v0.1.0
preview_version: preview.20260810
components:
  accelerator: v0.2.0-preview.20260810
  connector: v0.2.0-preview.20260810
  sandboxer: v0.2.0-preview.20260810
  orchestrator: v0.2.0-preview.20260810
  runtime: runtime-v0.2.0-preview.20260810
  vmlinux: vmlinux-v0.2.0-preview.20260810
EOF
[ "$("$history_root/release/selection.py" "$history_root" release-v0.2.0 --previous)" \
    = release-v0.1.0 ] \
  || release_fail "formal release did not select the previous formal release"
[ "$("$history_root/release/selection.py" "$history_root" \
    release-v0.2.0-preview.20260810 --previous)" = release-v0.1.0 ] \
  || release_fail "first preview did not use previous_version as its baseline"
"$history_root/release/selection.py" "$history_root" release-v0.1.0 \
  > "$TMP/historical-formal-selection.tsv"

git -C "$history_root" add releases/release.yaml releases/daily-preview.yaml
git -C "$history_root" commit -qm 'advance release line'
sed -i 's/preview\.20260810/preview.20260811/g' "$history_root/releases/daily-preview.yaml"
sed -i '/^preview_version:/a previous_preview_version: preview.20260810' \
  "$history_root/releases/daily-preview.yaml"
[ "$("$history_root/release/selection.py" "$history_root" \
    release-v0.2.0-preview.20260811 --previous)" \
    = release-v0.2.0-preview.20260810 ] \
  || release_fail "later preview did not use previous_preview_version as its baseline"
"$history_root/release/selection.py" "$history_root" release-v0.2.0-preview.20260810 \
  > "$TMP/historical-preview-selection.tsv"

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
  bash "$FORMAL_ROOT/release/preview-coordinator.sh" --version "$PREVIEW_VERSION" \
  > "$TMP/coordinator-error.out" 2>&1; then
  release_fail "preview coordinator treated a release API error as a missing release"
fi
[ ! -e "$TMP/coordinator-error-state/dispatches" ] \
  || release_fail "preview coordinator dispatched a workflow after a release API error"

PATH="$TMP/coordinator-bin:$PATH" \
  FAKE_COORDINATOR_STATE="$TMP/coordinator-state" \
  PREVIEW_POLL_SECONDS=0 PREVIEW_WAIT_SECONDS=0 \
  bash "$FORMAL_ROOT/release/preview-coordinator.sh" "$PREVIEW_DATE" \
  > "$TMP/coordinator.out"
[ "$(wc -l < "$TMP/coordinator-state/dispatches")" -eq 3 ] \
  || release_fail "preview coordinator did not dispatch the three foundation release units first"
grep -Fqx "workflow run release.yml --repo kuasar-sandbox/accelerator --ref main -f version=$PREVIEW_ACCELERATOR_TAG" \
  "$TMP/coordinator-state/dispatches" \
  || release_fail "preview coordinator did not dispatch the expected accelerator release"
grep -Fq 'preview remains pending' "$TMP/coordinator.out" \
  || release_fail "preview coordinator did not preserve pending convergence"

mkdir -p "$TMP/generated-root" "$TMP/generated-bin" "$TMP/generated-state"
cp -a "$ROOT/release" "$ROOT/releases" "$TMP/generated-root/"
current_formal_manifest="$TMP/current-release.yaml"
install -m 0644 "$ROOT/releases/release.yaml" "$current_formal_manifest"
write_preview_base_fixture "$TMP/generated-root/releases/release.yaml"
git -C "$TMP/generated-root" init -q
git -C "$TMP/generated-root" config user.name release-test
git -C "$TMP/generated-root" config user.email release-test@example.invalid
git -C "$TMP/generated-root" add release releases
git -C "$TMP/generated-root" commit -qm 'maintain preview base selection'
install -m 0644 "$current_formal_manifest" \
  "$TMP/generated-root/releases/release.yaml"
if ! git -C "$TMP/generated-root" diff --quiet -- releases/release.yaml; then
  git -C "$TMP/generated-root" add releases/release.yaml
  git -C "$TMP/generated-root" commit -qm 'advance formal release selection'
fi
install -m 0644 "$TMP/generated-root/releases/daily-preview.yaml" \
  "$TMP/generated-state/current-daily-preview.yaml"
cat > "$TMP/generated-bin/gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

state="${FAKE_COORDINATOR_STATE:?}"
root="${FAKE_COORDINATOR_ROOT:?}"
current_aggregate="${FAKE_CURRENT_AGGREGATE:?}"
current_manifest="$state/current-daily-preview.yaml"

selected_tag() {
  awk -v key="$1:" '$1 == key {print $2}' "$current_manifest"
}

archive_name() {
  local repository="$1" tag="$2" component="${repository##*/}"
  case "$component:$tag" in
    guest-runtime:runtime-*) printf 'sandbox-runtime-x86_64-%s.tar.gz\n' "${tag#runtime-}" ;;
    guest-runtime:vmlinux-*) printf 'vmlinux-x86_64-%s.tar.gz\n' "${tag#vmlinux-}" ;;
    *) printf '%s-%s-linux-x86_64.tar.gz\n' "$component" "$tag" ;;
  esac
}

component_release() {
  local repository="$1" tag="$2" archive prerelease=false
  archive="$(archive_name "$repository" "$tag")"
  [[ "$tag" == *-preview.* ]] && prerelease=true
  jq -n --arg tag "$tag" --arg archive "$archive" --argjson prerelease "$prerelease" '
    {tag_name: $tag, draft: false, prerelease: $prerelease,
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

  if [ "$method" = PUT ] && [[ "$endpoint" == repos/kuasar-sandbox/kuasar-sandbox/contents/releases/* ]]; then
    cp "$input" "$state/manifest-request.json"
    printf '%s\n' "$endpoint" > "$state/manifest-endpoint"
    printf '{"commit":{"sha":"2222222222222222222222222222222222222222"}}\n'
    exit 0
  fi

  case "$endpoint" in
    repos/kuasar-sandbox/kuasar-sandbox/contents/releases/daily-preview.yaml\?ref=main)
      jq -n --arg content "$(base64 -w0 "$root/releases/daily-preview.yaml")" \
        '{sha: "1111111111111111111111111111111111111111", content: $content}'
      ;;
    repos/*/releases\?*)
      repository="${endpoint#repos/}"
      repository="${repository%%/releases*}"
      if [ "$repository" = kuasar-sandbox/guest-runtime ]; then
        printf '[[%s,%s]]\n' \
          "$(component_release "$repository" "$(selected_tag runtime)")" \
          "$(component_release "$repository" "$(selected_tag vmlinux)")"
      else
        unit="${repository##*/}"
        printf '[[%s]]\n' \
          "$(component_release "$repository" "$(selected_tag "$unit")")"
      fi
      ;;
    repos/*/releases/tags/*)
      repository="${endpoint#repos/}"
      repository="${repository%%/releases/tags/*}"
      tag="${endpoint##*/}"
      unit="${repository##*/}"
      if [ "$repository" = kuasar-sandbox/guest-runtime ]; then
        case "$tag" in
          runtime-*) unit=runtime ;;
          vmlinux-*) unit=vmlinux ;;
        esac
      fi
      missing=false
      case ",${FAKE_MISSING_UNITS:-}," in
        *",$unit,"*) missing=true ;;
      esac
      if [ "$missing" = true ]; then
        echo 'gh: Not Found (HTTP 404)' >&2
        exit 1
      elif [ "$repository" = kuasar-sandbox/kuasar-sandbox ] && [ "$tag" = "$current_aggregate" ]; then
        jq -n --arg tag "$tag" \
          '{tag_name: $tag, draft: false, prerelease: true, published_at: "2026-08-09T00:00:00Z"}'
      elif [ "$repository" = kuasar-sandbox/guest-runtime ] \
        && { [ "$tag" = "$(selected_tag runtime)" ] \
          || [ "$tag" = "$(selected_tag vmlinux)" ]; }; then
        component_release "$repository" "$tag"
      elif [ "$repository" != kuasar-sandbox/kuasar-sandbox ] \
        && [ "$tag" = "$(selected_tag "${repository##*/}")" ]; then
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
    repository="${url#*/repos/kuasar-sandbox/}"
    repository="${repository%%/compare/*}"
    case ",${FAKE_CHANGED_REPOSITORIES-accelerator}," in
      *",$repository,"*) printf '{"status":"ahead"}\n' ;;
      *) printf '{"status":"identical"}\n' ;;
    esac
    ;;
  *)
    echo "fake generated coordinator curl: unsupported URL: $url" >&2
    exit 2
    ;;
esac
EOF
chmod +x "$TMP/generated-bin/curl"

mkdir -p "$TMP/unchanged-state"
install -m 0644 "$TMP/generated-root/releases/daily-preview.yaml" \
  "$TMP/unchanged-state/current-daily-preview.yaml"
PATH="$TMP/generated-bin:$PATH" \
  FAKE_COORDINATOR_STATE="$TMP/unchanged-state" \
  FAKE_COORDINATOR_ROOT="$TMP/generated-root" \
  FAKE_CURRENT_AGGREGATE="$PREVIEW_VERSION" \
  FAKE_CHANGED_REPOSITORIES='' \
  GH_TOKEN=read-token PLATFORM_TOKEN=write-token \
  PREVIEW_POLL_SECONDS=0 PREVIEW_WAIT_SECONDS=0 \
  bash "$TMP/generated-root/release/preview-coordinator.sh" "$NEXT_PREVIEW_DATE" \
  > "$TMP/unchanged-coordinator.out"
cmp -s "$TMP/generated-root/releases/daily-preview.yaml" \
  "$TMP/unchanged-state/current-daily-preview.yaml" \
  || release_fail "unchanged components advanced the daily preview manifest"
[ ! -e "$TMP/unchanged-state/manifest-request.json" ] \
  || release_fail "unchanged components committed a daily preview manifest"
[ ! -e "$TMP/unchanged-state/dispatches" ] \
  || release_fail "unchanged components dispatched a release workflow"
grep -Fq "no component changes since $PREVIEW_VERSION; keep maintained preview" \
  "$TMP/unchanged-coordinator.out" \
  || release_fail "daily coordinator did not report the unchanged preview no-op"
[ -z "$(git -C "$TMP/generated-root" status --porcelain)" ] \
  || release_fail "unchanged components modified the preview checkout"

PATH="$TMP/generated-bin:$PATH" \
  FAKE_COORDINATOR_STATE="$TMP/generated-state" \
  FAKE_COORDINATOR_ROOT="$TMP/generated-root" \
  FAKE_CURRENT_AGGREGATE="$PREVIEW_VERSION" \
  FAKE_CHANGED_REPOSITORIES=accelerator \
  GH_TOKEN=read-token PLATFORM_TOKEN=write-token \
  PREVIEW_POLL_SECONDS=0 PREVIEW_WAIT_SECONDS=0 \
  bash "$TMP/generated-root/release/preview-coordinator.sh" "$NEXT_PREVIEW_DATE" \
  > "$TMP/generated-coordinator.out"

GENERATED_VERSION="$PREVIEW_BASE-preview.$NEXT_PREVIEW_DATE"
GENERATED_ACCELERATOR_TAG="$PREVIEW_COMPONENT_VERSION-preview.$NEXT_PREVIEW_DATE"
GENERATED_SANDBOXER_TAG="$PREVIEW_COMPONENT_VERSION-preview.$NEXT_PREVIEW_DATE"
GENERATED_ORCHESTRATOR_TAG="$PREVIEW_COMPONENT_VERSION-preview.$NEXT_PREVIEW_DATE"
GENERATED_RUNTIME_TAG="runtime-$PREVIEW_COMPONENT_VERSION-preview.$NEXT_PREVIEW_DATE"
GENERATED_MANIFEST="$TMP/generated-root/releases/daily-preview.yaml"
[ -f "$GENERATED_MANIFEST" ] || release_fail "daily coordinator did not generate a manifest"
[ "$(previous_release "$TMP/generated-root" "$GENERATED_VERSION")" = "$PREVIEW_VERSION" ] \
  || release_fail "generated manifest does not name the latest aggregate as previous"
resolve_selection "$TMP/generated-root" "$GENERATED_VERSION" "$TMP/generated-selection.tsv"
grep -Fqx "accelerator"$'\t'"$GENERATED_ACCELERATOR_TAG" "$TMP/generated-selection.tsv" \
  || release_fail "changed component did not select a new preview"
grep -Fqx "sandboxer"$'\t'"$GENERATED_SANDBOXER_TAG" "$TMP/generated-selection.tsv" \
  || release_fail "accelerator change did not rebuild sandboxer"
grep -Fqx "orchestrator"$'\t'"$GENERATED_ORCHESTRATOR_TAG" "$TMP/generated-selection.tsv" \
  || release_fail "accelerator change did not rebuild orchestrator"
grep -Fqx "runtime"$'\t'"$GENERATED_RUNTIME_TAG" "$TMP/generated-selection.tsv" \
  || release_fail "accelerator change did not rebuild runtime"
for unit in connector vmlinux; do
  current_tag="$(awk -F '\t' -v unit="$unit" '$1 == unit {print $2}' \
    "$TMP/current-preview-selection.tsv")"
  grep -Fqx "$unit"$'\t'"$current_tag" "$TMP/generated-selection.tsv" \
    || release_fail "$unit did not reuse its unchanged release"
done
[ "$(cat "$TMP/generated-state/manifest-endpoint")" = \
  "repos/kuasar-sandbox/kuasar-sandbox/contents/releases/daily-preview.yaml" ] \
  || release_fail "generated manifest was committed to an unexpected path"
jq -e '.sha == "1111111111111111111111111111111111111111"' \
  "$TMP/generated-state/manifest-request.json" >/dev/null \
  || release_fail "daily coordinator did not use the current blob SHA"
jq -r .content "$TMP/generated-state/manifest-request.json" | base64 -d \
  > "$TMP/persisted-manifest"
cmp -s "$GENERATED_MANIFEST" "$TMP/persisted-manifest" \
  || release_fail "committed manifest differs from the frozen local selection"
[ "$(wc -l < "$TMP/generated-state/dispatches")" -eq 1 ] \
  || release_fail "daily coordinator dispatched an unchanged release unit"
grep -Fqx "workflow run release.yml --repo kuasar-sandbox/accelerator --ref main -f version=$GENERATED_ACCELERATOR_TAG" \
  "$TMP/generated-state/dispatches" \
  || release_fail "daily coordinator did not dispatch the changed release unit"

install -m 0644 "$GENERATED_MANIFEST" "$TMP/generated-state/current-daily-preview.yaml"
GENERATED_CONNECTOR_TAG="$(awk -F '\t' '$1 == "connector" {print $2}' \
  "$TMP/generated-selection.tsv")"

mkdir -p "$TMP/sandboxer-stage-state"
install -m 0644 "$GENERATED_MANIFEST" \
  "$TMP/sandboxer-stage-state/current-daily-preview.yaml"
PATH="$TMP/generated-bin:$PATH" \
  FAKE_COORDINATOR_STATE="$TMP/sandboxer-stage-state" \
  FAKE_COORDINATOR_ROOT="$TMP/generated-root" \
  FAKE_CURRENT_AGGREGATE="$PREVIEW_VERSION" \
  FAKE_MISSING_UNITS=sandboxer,orchestrator,runtime \
  GH_TOKEN=read-token PLATFORM_TOKEN=write-token \
  PREVIEW_POLL_SECONDS=0 PREVIEW_WAIT_SECONDS=0 \
  bash "$TMP/generated-root/release/preview-coordinator.sh" \
    --version "$GENERATED_VERSION" > "$TMP/sandboxer-stage.out"
[ "$(wc -l < "$TMP/sandboxer-stage-state/dispatches")" -eq 1 ] \
  || release_fail "dependency convergence did not isolate the sandboxer stage"
grep -Fqx "workflow run release.yml --repo kuasar-sandbox/sandboxer --ref main -f version=$GENERATED_SANDBOXER_TAG -f accelerator_version=$GENERATED_ACCELERATOR_TAG -f connector_version=$GENERATED_CONNECTOR_TAG" \
  "$TMP/sandboxer-stage-state/dispatches" \
  || release_fail "sandboxer dispatch did not pin selected dependency versions"

mkdir -p "$TMP/downstream-stage-state"
install -m 0644 "$GENERATED_MANIFEST" \
  "$TMP/downstream-stage-state/current-daily-preview.yaml"
PATH="$TMP/generated-bin:$PATH" \
  FAKE_COORDINATOR_STATE="$TMP/downstream-stage-state" \
  FAKE_COORDINATOR_ROOT="$TMP/generated-root" \
  FAKE_CURRENT_AGGREGATE="$PREVIEW_VERSION" \
  FAKE_MISSING_UNITS=orchestrator,runtime \
  GH_TOKEN=read-token PLATFORM_TOKEN=write-token \
  PREVIEW_POLL_SECONDS=0 PREVIEW_WAIT_SECONDS=0 \
  bash "$TMP/generated-root/release/preview-coordinator.sh" \
    --version "$GENERATED_VERSION" > "$TMP/downstream-stage.out"
[ "$(wc -l < "$TMP/downstream-stage-state/dispatches")" -eq 2 ] \
  || release_fail "dependency convergence did not isolate the final downstream stage"
grep -Fqx "workflow run component-release.yml --repo kuasar-sandbox/orchestrator --ref main -f version=$GENERATED_ORCHESTRATOR_TAG -f accelerator_version=$GENERATED_ACCELERATOR_TAG -f connector_version=$GENERATED_CONNECTOR_TAG -f sandboxer_version=$GENERATED_SANDBOXER_TAG" \
  "$TMP/downstream-stage-state/dispatches" \
  || release_fail "orchestrator dispatch did not pin selected dependency versions"
grep -Fqx "workflow run release-runtime.yml --repo kuasar-sandbox/guest-runtime --ref main -f version=$GENERATED_RUNTIME_TAG -f accelerator_version=$GENERATED_ACCELERATOR_TAG -f connector_version=$GENERATED_CONNECTOR_TAG -f sandboxer_version=$GENERATED_SANDBOXER_TAG" \
  "$TMP/downstream-stage-state/dispatches" \
  || release_fail "runtime dispatch did not pin selected dependency versions"

PATH="$TMP/generated-bin:$PATH" \
  FAKE_COORDINATOR_STATE="$TMP/generated-state" \
  FAKE_COORDINATOR_ROOT="$TMP/generated-root" \
  FAKE_CURRENT_AGGREGATE="$PREVIEW_VERSION" \
  FAKE_CHANGED_REPOSITORIES=accelerator \
  GH_TOKEN=read-token PLATFORM_TOKEN=write-token \
  PREVIEW_POLL_SECONDS=0 PREVIEW_WAIT_SECONDS=0 \
  bash "$TMP/generated-root/release/preview-coordinator.sh" "$NEXT_PREVIEW_DATE" \
  > "$TMP/generated-aggregate-coordinator.out"
[ "$(wc -l < "$TMP/generated-state/dispatches")" -eq 2 ] \
  || release_fail "daily coordinator did not dispatch exactly one changed component and one aggregate"
grep -Fqx "workflow run aggregate-release.yml --repo kuasar-sandbox/kuasar-sandbox --ref main -f version=$GENERATED_VERSION" \
  "$TMP/generated-state/dispatches" \
  || release_fail "daily coordinator did not dispatch an aggregate for the changed selection"

echo "test-release: PASS"
