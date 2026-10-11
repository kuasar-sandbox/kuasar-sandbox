#!/usr/bin/env bash
# Platform-owned external tools; component recipes live in their owner sources.
set -euo pipefail
[ "$#" = 2 ] || { echo 'usage: build-platform-helpers.sh ARCH OUTPUT' >&2; exit 2; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
case "$1" in x86_64|aarch64) ;; *) exit 2 ;; esac
TARGET_ARCH="$1" KUASAR_E2E_TOOL_OUTPUT="$2" bash "$ROOT/ci/hosted/exact-assets-tools.sh"
