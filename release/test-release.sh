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
    printf 'test_revisions:\n'
    for owner in accelerator connector guest-runtime sandboxer orchestrator; do
      printf '  %s: %040d\n' "$owner" 1
    done
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
mkdir -p "$TMP/fetched/test-sources"
python3 "$ROOT/release/selection.py" "$FORMAL_ROOT" "$VERSION" --test-revisions \
  > "$TMP/fetched/test-revisions.json"
install -m 0644 "$TMP/selection.tsv" "$TMP/fetched/selection.tsv"
: > "$TMP/fetched/previous-selection.tsv"

while IFS=$'\t' read -r unit tag; do
  archive="$(component_archive "$unit" "$tag")"
  stage="$TMP/stage-$unit"
  directory="$TMP/fetched/components/$unit"
  mkdir -p "$stage/bin" "$directory" \
    "$stage/share/licenses/$unit/project" "$stage/share/sources/$unit"
  printf '%s %s\n' "$unit" "$tag" > "$stage/bin/$unit"
  chmod +x "$stage/bin/$unit"
  printf '%s license fixture\n' "$unit" \
    > "$stage/share/licenses/$unit/project/LICENSE"
  printf 'payload\tname\n%s\t%s\n' "$unit" "$unit" \
    > "$stage/share/sources/$unit/SOURCES.tsv"
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
  if [ "$unit" != vmlinux ]; then
    owner="$unit"
    [ "$owner" != runtime ] || owner=guest-runtime
    cp -a "$source_root" "$TMP/fetched/test-sources/$owner"
  fi
done < "$TMP/selection.tsv"
printf '#!/usr/bin/env bash\necho "pinned orchestrator E2E"\n' \
  > "$TMP/fetched/test-sources/orchestrator/test/e2e/run_all.sh"
# Model the current mixed #172 migration: accelerator is case-only, while the
# other fixtures retain the legacy owner entry point.
accelerator_suite="$TMP/fetched/test-sources/accelerator/test/e2e"
rm "$accelerator_suite/run_all.sh"
mkdir -p "$accelerator_suite/cases"
cat > "$accelerator_suite/cases/storage.fixture.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
echo 'accelerator case-only fixture'
EOF
chmod 0644 "$accelerator_suite/cases/storage.fixture.sh"
connector_suite="$TMP/fetched/test-sources/connector/test/e2e"
mkdir -p "$connector_suite/cases"
printf '#!/usr/bin/env bash\necho connector-case\n' \
  > "$connector_suite/cases/connector.fixture.sh"
chmod 0644 "$connector_suite/cases/connector.fixture.sh"
printf 'runtime copy of vmlinux docs\n' > "$TMP/fetched/sources/runtime/docs/vmlinux.md"
printf 'runtime copy of Chinese vmlinux docs\n' > "$TMP/fetched/sources/runtime/docs/vmlinux_zh.md"
printf 'selected vmlinux docs\n' > "$TMP/fetched/sources/vmlinux/docs/vmlinux.md"
printf 'selected Chinese vmlinux docs\n' > "$TMP/fetched/sources/vmlinux/docs/vmlinux_zh.md"

cp -a "$TMP/fetched" "$TMP/fetched-foreign-material"
foreign_unit=connector
foreign_tag="$(awk -F '\t' -v unit="$foreign_unit" '$1 == unit {print $2}' \
  "$TMP/selection.tsv")"
foreign_archive="$(component_archive "$foreign_unit" "$foreign_tag")"
foreign_dir="$TMP/fetched-foreign-material/components/$foreign_unit"
mkdir -p "$TMP/foreign-stage"
tar -xzf "$foreign_dir/$foreign_archive" -C "$TMP/foreign-stage"
mkdir -p "$TMP/foreign-stage/share/licenses/accelerator/injected"
printf 'foreign namespace fixture\n' \
  > "$TMP/foreign-stage/share/licenses/accelerator/injected/LICENSE"
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime='@1700000000' \
  -czf "$foreign_dir/$foreign_archive" -C "$TMP/foreign-stage" .
(cd "$foreign_dir" && sha256sum "$foreign_archive" > SHA256SUMS)
if SOURCE_DATE_EPOCH=1700000000 PLATFORM_SOURCE_ROOT="$FORMAL_ROOT" \
  "$ROOT/release/aggregate-release.sh" assemble "$VERSION" \
    "$TMP/fetched-foreign-material" "$TMP/foreign-bundle" \
    >"$TMP/foreign-material.out" 2>&1; then
  release_fail "aggregate accepted a component's foreign material namespace"
fi
grep -Fq "$foreign_unit archive contains another release unit's material namespace" \
  "$TMP/foreign-material.out" \
  || release_fail "foreign material fixture failed outside the namespace check"

SOURCE_DATE_EPOCH=1700000000 PLATFORM_SOURCE_ROOT="$FORMAL_ROOT" \
  "$ROOT/release/aggregate-release.sh" assemble "$VERSION" "$TMP/fetched" "$TMP/bundle"
mkdir -p "$TMP/generated-runner"
tar -xzf "$TMP/bundle/assets/$(platform_archive "$VERSION")" \
  -C "$TMP/generated-runner" ./test/e2e/accelerator/run_all.sh
generated_accelerator_runner="$TMP/generated-runner/test/e2e/accelerator/run_all.sh"
[ -x "$generated_accelerator_runner" ] \
  || release_fail "case-only owner compatibility runner is not executable"
grep -Fq -- '--include storage.fixture.sh' "$generated_accelerator_runner" \
  || release_fail "case-only owner runner omits its exact case"
if grep -Fq -- '--include connector.fixture.sh' "$generated_accelerator_runner"; then
  release_fail "case-only owner runner can select another owner's case"
fi
tar -xOf "$TMP/bundle/assets/$(platform_archive "$VERSION")" ./test/e2e/orchestrator/run_all.sh \
  > "$TMP/packaged-orchestrator-test"
cmp "$TMP/packaged-orchestrator-test" "$TMP/fetched/test-sources/orchestrator/test/e2e/run_all.sh"
if cmp -s "$TMP/packaged-orchestrator-test" "$TMP/fetched/sources/orchestrator/test/e2e/run_all.sh"; then
  release_fail "platform package used the product tag's test instead of the independent pin"
fi

assert_assembly_rejected() {
  local name=$1 expected=$2 tests output
  tests="$TMP/tests-$name"
  output="$TMP/output-$name"
  cp -a "$TMP/fetched/test-sources" "$tests"
  shift 2
  "$@" "$tests"
  if E2E_SOURCE_ROOT="$tests" "$ROOT/test/e2e/assemble.sh" "$output" \
      "$FORMAL_ROOT" "$TMP/fetched/sources/accelerator" \
      "$TMP/fetched/sources/connector" "$TMP/fetched/sources/runtime" \
      "$TMP/fetched/sources/sandboxer" "$TMP/fetched/sources/orchestrator" \
      > "$TMP/$name.log" 2>&1; then
    release_fail "assembler accepted $name fixture"
  fi
  grep -Fq "$expected" "$TMP/$name.log" \
    || release_fail "$name fixture failed outside its intended guard"
}

remove_owner_inputs() {
  rm -f "$1/connector/test/e2e/run_all.sh"
  rm -rf "$1/connector/test/e2e/cases"
}
assert_assembly_rejected missing-owner \
  'connector source is missing valid test/e2e/cases/*.sh and executable test/e2e/run_all.sh' \
  remove_owner_inputs

add_duplicate_case() {
  mkdir -p "$1/connector/test/e2e/cases"
  cp "$1/accelerator/test/e2e/cases/storage.fixture.sh" \
    "$1/connector/test/e2e/cases/storage.fixture.sh"
}
assert_assembly_rejected duplicate-case \
  'duplicate E2E case id from connector: storage.fixture.sh' add_duplicate_case

add_suite_symlink() {
  ln -s run_all.sh "$1/sandboxer/test/e2e/linked-runner"
}
assert_assembly_rejected suite-symlink \
  'sandboxer e2e suite contains a symbolic link' add_suite_symlink
while IFS=$'\t' read -r unit tag; do
  archive="$(component_archive "$unit" "$tag")"
  cmp "$TMP/fetched/components/$unit/$archive" "$TMP/bundle/assets/$archive"
done < "$TMP/selection.tsv"
cp "$TMP/fetched/test-revisions.json" "$TMP/test-pins-saved.json"
printf '{}\n' > "$TMP/fetched/test-revisions.json"
if SOURCE_DATE_EPOCH=1700000000 PLATFORM_SOURCE_ROOT="$FORMAL_ROOT" \
  "$ROOT/release/aggregate-release.sh" assemble "$VERSION" "$TMP/fetched" "$TMP/rejected-pins" \
  > "$TMP/rejected-pins.log" 2>&1; then
  release_fail "aggregate accepted mismatched test pins"
fi
grep -Fq 'fetched test pins do not match' "$TMP/rejected-pins.log"
mv "$TMP/test-pins-saved.json" "$TMP/fetched/test-revisions.json"
if PLATFORM_SOURCE_ROOT="$FORMAL_ROOT" "$ROOT/release/package-platform.sh" \
  package "$VERSION" "$TMP/fetched/sources" "" "$TMP/rejected-test-source" \
  > "$TMP/rejected-test-source.log" 2>&1; then
  release_fail "platform package silently fell back to product test sources"
fi
grep -Fq 'independently pinned test source directory is missing' "$TMP/rejected-test-source.log"
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
grep -Fqx 'selected Chinese vmlinux docs' "$TMP/install/docs/vmlinux_zh.md" \
  || release_fail "Chinese vmlinux docs did not come from the selected vmlinux source"

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
  [ "$(cat "$TMP/install/share/licenses/$unit/project/LICENSE")" \
      = "$unit license fixture" ] \
    || release_fail "$unit license material was overwritten during aggregate extraction"
  [ -s "$TMP/install/share/sources/$unit/SOURCES.tsv" ] \
    || release_fail "$unit source material was not extracted"
done

# A new dual aggregate contains both namespaces, but extraction chooses one.
# These packaging fixtures are not executable product/architecture acceptance.
cp -a "$TMP/fetched" "$TMP/fetched-dual"
while IFS=$'\t' read -r unit tag; do
  directory="$TMP/fetched-dual/components/$unit"
  archive="$(component_archive "$unit" "$tag" aarch64)"
  printf '%s ARM fixture\n' "$unit" > "$TMP/stage-$unit/bin/$unit"
  tar --sort=name --owner=0 --group=0 --numeric-owner --mtime='@1700000000' \
    -czf "$directory/$archive" -C "$TMP/stage-$unit" .
  (cd "$directory" && sha256sum ./*.tar.gz | sed 's@  ./@  @' > SHA256SUMS)
done < "$TMP/selection.tsv"
SOURCE_DATE_EPOCH=1700000000 PLATFORM_SOURCE_ROOT="$FORMAL_ROOT" \
  "$ROOT/release/aggregate-release.sh" assemble "$VERSION" "$TMP/fetched-dual" "$TMP/dual-bundle"
[ "$(find "$TMP/dual-bundle/assets" -maxdepth 1 -type f | wc -l)" -eq 14 ] \
  || release_fail 'dual aggregate does not contain both exact architecture sets'
for arch in x86_64 aarch64; do
  "$FORMAL_ROOT/release/aggregate-release.sh" extract "$VERSION" "$TMP/dual-bundle" "$TMP/install-$arch" "$arch"
  for unit in "${RELEASE_UNITS[@]}"; do
    if [ "$arch" = x86_64 ]; then
      cmp "$TMP/install/bin/$unit" "$TMP/install-$arch/bin/$unit" \
        || release_fail 'AMD64 baseline bytes changed during dual assembly'
    else
      grep -Fxq "$unit ARM fixture" "$TMP/install-$arch/bin/$unit" \
        || release_fail 'ARM extraction contains the other architecture'
    fi
  done
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
grep -Fq '"$VERSION" --commit' "$ROOT/.github/workflows/aggregate-release.yml" \
  || release_fail "aggregate workflow accepts a historical Stable selection"
grep -Fq 'run-name: Aggregate ${{ inputs.version }} @${{ inputs.source_sha }}' \
  "$ROOT/.github/workflows/aggregate-release.yml" \
  || release_fail "aggregate workflow run identity does not pin its source commit"
grep -Fq 'path: control' "$ROOT/.github/workflows/aggregate-release.yml" \
  || release_fail "aggregate prepare does not isolate trusted main tooling"
grep -Fq 'PLATFORM_SOURCE_ROOT:' "$ROOT/.github/workflows/aggregate-release.yml" \
  || release_fail "aggregate workflow does not separate target platform source"
grep -Fq 'PLATFORM_REF:' "$ROOT/.github/workflows/daily-preview-branch.yml" \
  || release_fail "branch converger does not pass the selected platform ref"
grep -Fq 'id: platform-writer-token' "$ROOT/.github/workflows/daily-preview-branch.yml" \
  || release_fail "branch converger does not request a dedicated platform writer token"
grep -Fq 'permission-contents: write' "$ROOT/.github/workflows/daily-preview-branch.yml" \
  || release_fail "branch converger cannot request platform write permission"
grep -Fq 'PLATFORM_TOKEN: ${{ steps.platform-writer-token.outputs.token }}' \
  "$ROOT/.github/workflows/daily-preview-branch.yml" \
  || release_fail "branch converger does not use the dedicated platform writer token"
if grep -Fq 'PLATFORM_TOKEN: ${{ github.token }}' \
  "$ROOT/.github/workflows/daily-preview-branch.yml"; then
  release_fail "branch converger still writes with the generic GitHub Actions token"
fi
for workflow in daily-preview-branch.yml preview-gc.yml; do
  grep -Fq 'group: preview-manifest-selection-and-gc' \
    "$ROOT/.github/workflows/$workflow" \
    || release_fail "$workflow does not serialize manifest selection with GC"
done
if grep -R -Fq 'queue: max' "$ROOT/.github/workflows"; then
  release_fail "release workflows use the unsupported concurrency queue key"
fi
grep -Fq 'matching_run()' "$ROOT/.github/workflows/daily-preview.yml" \
  || release_fail "Daily scanner does not wait for exact branch runs"
grep -Fq 'record branch failure and continue' \
  "$ROOT/.github/workflows/daily-preview.yml" \
  || release_fail "Daily scanner lets one branch starve later branches"
grep -Fq 'title="Daily preview $ref@$sha for $date"' \
  "$ROOT/.github/workflows/daily-preview.yml" \
  || release_fail "Daily scanner run identity does not include the requested date"
grep -Fq 'wait_for_gc()' "$ROOT/.github/workflows/daily-preview.yml" \
  || release_fail "Daily scanner does not preserve a pending GC operation"
for workflow in aggregate-release.yml delete-preview.yml; do
  [ "$(grep -Fc 'group: aggregate-mutation-${{ github.repository }}-${{ inputs.version }}' \
    "$ROOT/.github/workflows/$workflow")" -eq 1 ] \
    || release_fail "$workflow does not hold exactly one full-workflow mutation lock"
done
grep -Fq 'moved while release asset validation was running' \
  "$ROOT/.github/workflows/aggregate-release.yml" \
  || release_fail "aggregate publisher does not recheck source branch HEAD"
grep -Fq 'platform_source_sha: ${{ needs.prepare.outputs.source_sha }}' \
  "$ROOT/.github/workflows/aggregate-release.yml" \
  || release_fail "aggregate validation does not receive the selected platform source"
grep -Fq 'source_text(PLATFORM, sha, relative)' \
  "$ROOT/ci/integration/resolve-artifacts.py" \
  || release_fail "exact-asset validation does not validate against selected platform source"
grep -Fq 'ref: main' "$ROOT/.github/workflows/preview-gc.yml" \
  || release_fail "Preview GC does not pin trusted main tooling"
grep -Fq 'Preview GC must run from main' "$ROOT/.github/workflows/preview-gc.yml" \
  || release_fail "Preview GC accepts a non-main workflow ref"
grep -Fq 'validate-preview-line.sh" "$version" "$commit"' \
  "$ROOT/release/publish-release.sh" \
  || release_fail "aggregate publisher does not recheck Preview closure before undraft"

echo "test-release: PASS"
