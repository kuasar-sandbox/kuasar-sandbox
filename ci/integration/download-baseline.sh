#!/usr/bin/env bash
# Download only the selected target of one resolved aggregate, using the release
# downloader and the exact API size/digest plus aggregate checksum contract.
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
names.update(('SHA256SUMS', 'platform-' + baseline['version'] + '.tar.gz'))
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
PYTHONPATH="$ROOT/ci/integration" python3 - "$plan" "$output/SHA256SUMS" <<'PY'
import json, re, sys
import artifacts
plan = json.load(open(sys.argv[1]))
expected = {record['name']: record['digest'].removeprefix('sha256:')
            for record in plan['baseline']['assets'] if record['name'] != 'SHA256SUMS'}
actual = {}
for line in open(sys.argv[2]):
    match = re.fullmatch(r'([0-9a-f]{64}) [ *]([^\r\n]+)\n?', line)
    artifacts.require(match is not None, 'invalid aggregate checksum entry')
    digest, name = match.groups()
    artifacts.require(name not in actual, 'duplicate aggregate checksum entry')
    actual[name] = digest
artifacts.require(actual == expected, 'aggregate checksums differ from resolved API assets')
PY
