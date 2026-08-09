#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${BIN:?BIN must point to the assembled platform binary directory}"
export BIN REQUIRE_KVM=1

shopt -s nullglob
cases=("$SCRIPT_DIR"/e2e_*.sh)
[ "${#cases[@]}" -gt 0 ] || {
    echo "platform e2e suite contains no cases" >&2
    exit 1
}

for script in "${cases[@]}"; do
    echo
    echo "========================================="
    echo "  platform/$(basename "$script")"
    echo "========================================="
    bash "$script"
done
