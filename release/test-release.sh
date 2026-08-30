#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=release/lib.sh
source "$ROOT/release/lib.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

bash "$ROOT/release/test-preview-line.sh"
bash "$ROOT/release/test-delete-preview.sh"

PREVIEW_BASE="$(awk '$1 == "version:" {print $2}' "$ROOT/releases/daily-preview.yaml")"
PREVIEW_SUFFIX="$(awk '$1 == "preview_version:" {print $2}' "$ROOT/releases/daily-preview.yaml")"
PREVIEW_VERSION="$PREVIEW_BASE-$PREVIEW_SUFFIX"
resolve_selection "$ROOT" "$PREVIEW_VERSION" "$TMP/current-preview-selection.tsv"

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

SOURCE_DATE_EPOCH=1700000000 PLATFORM_SOURCE_ROOT="$FORMAL_ROOT" \
  "$ROOT/release/aggregate-release.sh" assemble "$VERSION" "$TMP/fetched" "$TMP/bundle"
PLATFORM_SOURCE_ROOT="$FORMAL_ROOT" \
  "$ROOT/release/aggregate-release.sh" validate "$VERSION" "$TMP/bundle"
PLATFORM_SOURCE_ROOT="$FORMAL_ROOT" \
  "$ROOT/release/test-publisher.sh" "$ROOT/release/publish-release.sh" \
  "$TMP/bundle" kuasar-sandbox/kuasar-sandbox "$VERSION" \
  1111111111111111111111111111111111111111 main

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

nongit_root="$TMP/nongit-selection"
mkdir -p "$nongit_root/release" "$nongit_root/releases"
install -m 0755 "$ROOT/release/selection.py" "$nongit_root/release/selection.py"
install -m 0644 "$ROOT/releases/release.yaml" "$nongit_root/releases/release.yaml"
install -m 0644 "$ROOT/releases/daily-preview.yaml" \
  "$nongit_root/releases/daily-preview.yaml"
"$nongit_root/release/selection.py" "$nongit_root" "$PREVIEW_VERSION" \
  > "$TMP/nongit-selection.tsv"
[ "$(wc -l < "$TMP/nongit-selection.tsv")" -eq 6 ] \
  || release_fail "current manifest selection unexpectedly requires Git metadata"
if "$nongit_root/release/selection.py" "$nongit_root" "$PREVIEW_VERSION" --commit \
  >/dev/null 2>&1; then
  release_fail "commit proof succeeded without Git metadata"
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

bash -n "$ROOT/release/preview-coordinator.sh" "$ROOT/release/delete-preview.sh"
python3 -m py_compile "$ROOT/release/selection.py" \
  "$ROOT/release/preview-selection.py" "$ROOT/release/preview_coordinator.py" \
  "$ROOT/release/formal_coordinator.py" "$ROOT/release/preview_gc.py"
for workflow in daily-preview.yml daily-preview-branch.yml aggregate-release.yml \
  delete-preview.yml preview-gc.yml; do
  [ -f "$ROOT/.github/workflows/$workflow" ] \
    || release_fail "missing release workflow: $workflow"
done
grep -Fq 'refs+=(main)' "$ROOT/.github/workflows/daily-preview.yml" \
  || release_fail "Daily scanner does not include platform main"
grep -Fq "grep -E '^release/v" "$ROOT/.github/workflows/daily-preview.yml" \
  || release_fail "Daily scanner does not discover maintenance branches"
grep -Fq 'source_sha:' "$ROOT/.github/workflows/aggregate-release.yml" \
  || release_fail "aggregate workflow does not pin its source commit"
grep -Fq 'path: control' "$ROOT/.github/workflows/aggregate-release.yml" \
  || release_fail "aggregate prepare does not isolate trusted main tooling"
grep -Fq 'PLATFORM_SOURCE_ROOT:' "$ROOT/.github/workflows/aggregate-release.yml" \
  || release_fail "aggregate workflow does not separate target platform source"
grep -Fq 'PLATFORM_REF:' "$ROOT/.github/workflows/daily-preview-branch.yml" \
  || release_fail "branch converger does not pass the selected platform ref"
grep -Fq 'queue: max' "$ROOT/.github/workflows/aggregate-release.yml" \
  || release_fail "aggregate publisher does not preserve queued mutations"
grep -Fq 'queue: max' "$ROOT/.github/workflows/delete-preview.yml" \
  || release_fail "aggregate cleanup does not preserve queued mutations"
grep -Fq 'validate-preview-line.sh" "$version" "$commit"' \
  "$ROOT/release/publish-release.sh" \
  || release_fail "aggregate publisher does not recheck Preview closure before undraft"

echo "test-release: PASS"
