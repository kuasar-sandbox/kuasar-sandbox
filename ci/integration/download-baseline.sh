#!/usr/bin/env bash
# Download only the selected target of one resolved aggregate, using the release
# downloader and the exact GitHub asset identity, size and digest.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$ROOT/release/lib.sh"
[ "$#" = 3 ] || [ "$#" = 4 ] || release_fail 'usage: download-baseline.sh <plan.json> <arch> <output> [staged-assets]'
plan=$1 arch=$2 output=$3 staged=${4:-}
case "$arch" in x86_64|aarch64) ;; *) release_fail 'invalid architecture' ;; esac
assert_safe_output "$output"
mkdir -p "$output"
records=$(mktemp)
trap 'rm -f "$records"' EXIT
PYTHONPATH="$ROOT/ci/integration" python3 - "$plan" "$arch" > "$records" <<'PY'
import json, sys
import artifacts
plan = json.load(open(sys.argv[1]))
artifacts.check_plan(plan)
baseline = plan['baseline']
artifacts.require(baseline['repository'] == 'kuasar-sandbox/kuasar-sandbox', 'foreign aggregate baseline')
names = {artifacts.archive_name(unit, record['version'], sys.argv[2]) for unit, record in baseline['units'].items()}
names.add('platform-' + baseline['version'] + '.tar.gz')
delivery = artifacts.delivery_module()
if baseline.get('delivery') == delivery.NODE_INFRA_DELIVERY and 'node_infra' not in plan:
    names.update((delivery.node_infra_archive(baseline['version'], sys.argv[2]),
                  delivery.node_infra_deploy_archive(baseline['version'])))
records = {record['name']: record for record in baseline['assets']}
artifacts.require(names <= set(records), 'missing target assets; explicit initialization is required')
for name in sorted(names):
    artifacts.relative(name)
    if not baseline.get('staged'):
        artifacts.require(isinstance(records[name]['id'], int) and records[name]['id'] > 0, 'invalid asset id')
    print(json.dumps(records[name], separators=(',', ':')))
PY
while IFS= read -r asset; do
    name=$(jq -er .name <<< "$asset")
    if [ -n "$staged" ]; then
        jq -e '.mode == "exact-assets" and .baseline.staged == true' "$plan" >/dev/null
        [ -f "$staged/$name" ] && [ ! -L "$staged/$name" ] || release_fail 'missing staged input'
        cp -- "$staged/$name" "$output/$name"
    else
        jq -e '.mode == "source" and .baseline.staged != true' "$plan" >/dev/null
        github_download_asset kuasar-sandbox/kuasar-sandbox "$(jq -er .id <<< "$asset")" "$output/$name"
    fi
    verify_github_asset "$output/$name" "$asset"
done < "$records"
