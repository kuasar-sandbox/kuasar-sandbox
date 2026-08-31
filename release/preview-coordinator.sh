#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

[ "$#" -le 1 ] || {
  echo "usage: preview-coordinator.sh [YYYYMMDD]" >&2
  exit 2
}

export PLATFORM_ROOT="${PLATFORM_ROOT:-$ROOT}"
export PLATFORM_REF="${PLATFORM_REF:-$(git -C "$PLATFORM_ROOT" branch --show-current)}"
export PLATFORM_SHA="${PLATFORM_SHA:-$(git -C "$PLATFORM_ROOT" rev-parse HEAD)}"
export PREVIEW_DATE="${1:-${PREVIEW_DATE:-$(TZ=Asia/Shanghai date +%Y%m%d)}}"

exec python3 "$ROOT/release/preview_coordinator.py"
