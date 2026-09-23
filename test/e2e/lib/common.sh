#!/usr/bin/env bash
set -euo pipefail

e2e_fail() {
    echo "FAIL: $*" >&2
    exit 1
}

require_binary() {
    local name=$1
    [ -x "${BIN:?BIN is required}/$name" ] || e2e_fail "missing prepared product: $name"
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || e2e_fail "missing required command: $1"
}

require_root() {
    [ "$(id -u)" -eq 0 ] || e2e_fail "root is required"
}

require_kvm() {
    [ -r /dev/kvm ] && [ -w /dev/kvm ] || e2e_fail "read/write /dev/kvm is required"
}

assert_contains() {
    local file=$1 text=$2
    grep -Fq -- "$text" "$file" || e2e_fail "$file does not contain: $text"
}
