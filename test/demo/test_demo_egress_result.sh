#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
say() { printf 'INFO: %s\n' "$*"; }
ok() { printf 'OK: %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; return 1; }
. "$ROOT/test/demo/demo_egress_result.sh"
expect_failure() {
    local name="$1" proxy="$2" expected="$3" output status
    set +e
    output="$(egress_handle_result 1 "probe output: $name" "$proxy" 2>&1)"
    status=$?
    set -e
    [ "$status" -ne 0 ] || { echo "FAIL: $name unexpectedly succeeded" >&2; exit 1; }
    [[ "$output" == *"probe output: $name"* ]] || { echo "FAIL: $name lost probe output" >&2; exit 1; }
    [[ "$output" == *"$expected"* ]] || { echo "FAIL: $name wrong failure reason: $output" >&2; exit 1; }
}
expect_failure direct '' 'guest outbound NAT validation failed'
expect_failure proxy 'http://proxy.demo:3128' 'guest outbound proxy validation failed'
for proxy in '' 'http://proxy.demo:3128'; do
    output="$(DEMO_NETDIAG=1 egress_handle_result 1 'diagnostic probe failure' "$proxy" 2>&1)"
    [[ "$output" == *'diagnostic probe failure'* && "$output" == *'NETDIAG: continuing'* ]] || { echo "FAIL: NETDIAG did not retain diagnostics" >&2; exit 1; }
done
echo 'PASS: failed direct/proxy egress probes are strict unless NETDIAG is set'
python3 - "$ROOT/test/demo/demo_e2b.sh" <<'PYWIRE'
from pathlib import Path
import sys
source = Path(sys.argv[1]).read_text()
needle = ')"; EGRESS_STATUS=$?\nset -e\negress_handle_result "$EGRESS_STATUS" "$EGRESS_OUTPUT" "$DEMO_EGRESS_PROXY"'
assert needle in source
PYWIRE
