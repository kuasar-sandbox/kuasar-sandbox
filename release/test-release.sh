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
for unit in "${RELEASE_UNITS[@]}"; do
  grep -Fqx "### $unit" "$TMP/bundle/release-notes.md" \
    || release_fail "aggregate release notes omit $unit updates"
done
if tar -tzf "$TMP/bundle/assets/$(platform_archive "$VERSION")" \
  | grep -E '(^|/)release\.json$|(^|/)release/[^/]+\.json$' >/dev/null; then
  release_fail "platform package contains release metadata JSON"
fi

"$ROOT/release/aggregate-release.sh" extract "$VERSION" "$TMP/bundle" "$TMP/install"
[ -f "$TMP/install/docs/kuasar-sandbox.md" ] || release_fail "platform docs were not extracted"
[ -x "$TMP/install/test/e2e/run_all.sh" ] || release_fail "platform E2E runner was not extracted"
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

echo "test-release: PASS"
