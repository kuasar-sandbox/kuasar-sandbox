#!/usr/bin/env bash
#
# Umbrella cluster e2e entry.
#
# This delegates to the orchestrator cluster stub suite shipped in the merged
# release tree. The delegated suite starts real
# cluster-ctl registry/router/placer processes and node-stub-ctl; node-stub-ctl
# simulates node-link, key distribution, sandbox/build commands, node reboot and
# data forwarding without launching microVMs.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOST_ARCH="$(uname -m)"
case "$HOST_ARCH" in amd64) HOST_ARCH=x86_64 ;; arm64) HOST_ARCH=aarch64 ;; esac
BIN="${BIN:-$REPO_ROOT/bin/$HOST_ARCH}"
ORCH_E2E="$REPO_ROOT/test/orchestrator/e2e_cluster_stub.sh"
if [ ! -f "$ORCH_E2E" ] && [ -f "$REPO_ROOT/../orchestrator/test/e2e/e2e_cluster_stub.sh" ]; then
  ORCH_E2E="$REPO_ROOT/../orchestrator/test/e2e/e2e_cluster_stub.sh"
fi
REQUIRE_CLUSTER_STUB="${REQUIRE_CLUSTER_STUB:-${REQUIRE_EXEC:-0}}"

skip() {
  echo "==> e2e_cluster: skipping ($*)" >&2
  if [ "$REQUIRE_CLUSTER_STUB" = "1" ]; then
    exit 1
  fi
  exit 0
}

[ -f "$ORCH_E2E" ] || skip "missing packaged orchestrator cluster stub suite at $ORCH_E2E"

echo "==> e2e_cluster: running packaged orchestrator cluster stub suite" >&2
echo "==> e2e_cluster: BIN=$BIN" >&2

BIN="$BIN" REQUIRE_CLUSTER_STUB="$REQUIRE_CLUSTER_STUB" bash "$ORCH_E2E"
