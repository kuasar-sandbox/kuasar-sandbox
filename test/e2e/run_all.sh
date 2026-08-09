#!/usr/bin/env bash
#
# Full source and release E2E gate. The source BMS assembler and the platform
# release packager both create the same test/e2e/<owner>/ directory layout.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASE_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BIN="${BIN:-$RELEASE_ROOT/bin}"
export BIN
: "${ZOT_BIN:?ZOT_BIN must point to the platform-provided registry}"
: "${VGW_BIN:?VGW_BIN must point to the platform-provided object gateway}"
export ZOT_BIN VGW_BIN

echo "==> full release e2e"
echo "==> BIN=$BIN"

owners=(accelerator connector guest-runtime sandboxer orchestrator platform)
for owner in "${owners[@]}"; do
    runner="$SCRIPT_DIR/$owner/run_all.sh"
    [ -x "$runner" ] || {
        echo "==> FAIL: missing executable E2E owner runner: $runner" >&2
        exit 1
    }
    bash "$runner"
done

echo
echo "==> full release e2e: OK"
