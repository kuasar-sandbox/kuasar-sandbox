#!/usr/bin/env bash
# Trusted host TEST tools only; never writes into the extracted product package.
set -euo pipefail

tooling_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=ci/hosted/bootstrap.sh
source "$tooling_dir/bootstrap.sh"

GUEST_TOOLS_COMMIT=494dbceae683d6b20cdbec00fe6b1f554ea2f508
VERSITYGW_SOURCE_SHA256=6a667e38d78a08effdf0b7c3c3e4b9929233b333c8a53c16b62eded7d43eeedc

fetch_recipe() {
    local path=$1 digest
    [[ "$GUEST_TOOLS_COMMIT" =~ ^[0-9a-f]{40}$ ]] || die "invalid guest tool commit"
    case "$path" in
        native-deps/deps/build-versitygw.sh) digest=9170b25cbddc2369738d07a69c1ef679aa30a5ae8ca1426b6f9b8450deb46f61 ;;
        native-deps/deps/common.sh) digest=e23ced7d2efa4f589af3a9eefd8ddaef7dacfa06919da883c26188bfb242e6cb ;;
        *) die "unapproved guest tool path: $path" ;;
    esac
    download "https://raw.githubusercontent.com/kuasar-sandbox/guest-runtime/$GUEST_TOOLS_COMMIT/$path" \
        "$tools_root/recipes/${path##*/}" "$digest"
    printf '%s\t%s\t%s\n' "$path" "$GUEST_TOOLS_COMMIT" "$digest" >> "$identity"
}

# Explicit environment paths take precedence over PATH and the fallback build.
# An invalid explicit path is an environment error, not a request to replace it.
environment_tool() {
    local name=$1 supplied=$2 selected
    if [ -n "$supplied" ]; then
        selected=$supplied
    elif [ "$target_arch" = "$(uname -m)" ]; then
        selected=$(command -v "$name" || true)
    else
        selected=""
    fi
    [ -n "$selected" ] || return 0
    [ -f "$selected" ] && [ -x "$selected" ] || die "environment $name is not executable: $selected"
    readlink -f "$selected"
}

main() {
    [ "$#" -eq 0 ] || die "usage: exact-assets-tools.sh (no arguments)"
    : "${RUNNER_TEMP:?}" "${GITHUB_ENV:?}" "${KUASAR_CI_DIR:?}"
    : "${KUASAR_BUILD_JOBS:?}" "${KUASAR_BUILD_CPUS:?}"
    [[ "$RUNNER_TEMP" = /* && "$RUNNER_TEMP" != *$'\n'* && -d "$RUNNER_TEMP" ]] \
        || die "RUNNER_TEMP must be an absolute existing directory"
    [[ "$KUASAR_BUILD_JOBS" =~ ^[1-9][0-9]*$ && "$KUASAR_BUILD_CPUS" =~ ^[0-9]+(,[0-9]+)*$ ]] \
        || die "invalid host tool build budget"
    local tools_root identity target_arch go_arch
    target_arch=${TARGET_ARCH:-$(uname -m)}
    case "$target_arch" in
        x86_64) go_arch=amd64 ;;
        aarch64) go_arch=arm64 ;;
        *) die "invalid target architecture: $target_arch" ;;
    esac
    tools_root=$(mktemp -d "$RUNNER_TEMP/kuasar-exact-tools.XXXXXX")
    identity="$KUASAR_CI_DIR/host-tools.tsv"
    mkdir -p "$tools_root"/{bin,recipes,tarballs,tmp} "$KUASAR_CI_DIR"
    printf 'input\tidentity\tsha256\n' > "$identity"
    local zot_bin vgw_bin
    zot_bin=$(environment_tool zot "${E2E_ZOT_BIN:-${ZOT_BIN:-}}")
    vgw_bin=$(environment_tool versitygw "${E2E_VGW_BIN:-${VGW_BIN:-}}")
    if [ -z "$vgw_bin" ]; then
        fetch_recipe native-deps/deps/build-versitygw.sh
        fetch_recipe native-deps/deps/common.sh

        local source_url=https://codeload.github.com/versity/versitygw/tar.gz/refs/tags/v1.5.0
        local archive="$tools_root/tarballs/versitygw-1.5.0.tar.gz"
        download "$source_url" "$archive" "$VERSITYGW_SOURCE_SHA256"
        # Validate paths/types before the reviewed native recipe sees the source.
        bash "$tooling_dir/../integration/materialize-source.sh" "$archive" "$tools_root/versitygw"
        touch "$tools_root/versitygw/.extracted"
        printf 'versitygw-source\t%s\t%s\n' "$source_url" "$VERSITYGW_SOURCE_SHA256" >> "$identity"
        BINDIR="$tools_root/bin" BUILD_DIR="$tools_root/build" \
            TARBALL_CACHE="$tools_root/tarballs" TMPDIR="$tools_root/tmp" \
            VERSITYGW_SRC="$tools_root/versitygw" VERSITYGW_TARBALL="$archive" \
            VERSITYGW_TARBALL_SHA256="$VERSITYGW_SOURCE_SHA256" \
            VERSITYGW_GOFLAGS="-mod=mod -p=$KUASAR_BUILD_JOBS" \
            GOMAXPROCS="$KUASAR_BUILD_JOBS" GO_ARCH="$go_arch" \
            taskset -c "$KUASAR_BUILD_CPUS" bash "$tools_root/recipes/build-versitygw.sh"
        vgw_bin="$tools_root/bin/versitygw"
    fi
    if [ -z "$zot_bin" ]; then
        BINDIR="$tools_root/bin" TARGET_ARCH="$target_arch" TARBALL_CACHE="$tools_root/tarballs" \
            ZOT_VERSION=v2.1.17 \
            ZOT_URL="https://github.com/project-zot/zot/releases/download/v2.1.17/zot-linux-$go_arch-minimal" \
            ZOT_SHA256_URL=https://github.com/project-zot/zot/releases/download/v2.1.17/checksums.sha256.txt \
            bash "$tooling_dir/../integration/ensure-zot.sh"
        zot_bin="$tools_root/bin/zot"
    fi
    local tool path
    for tool in zot versitygw; do
        if [ "$tool" = zot ]; then path=$zot_bin; else path=$vgw_bin; fi
        [ -x "$path" ] || die "missing host test tool: $tool"
        PYTHONPATH="$tooling_dir/../integration" python3 - "$path" "$target_arch" <<'PY'
import sys
from artifacts import check_architecture
check_architecture(sys.argv[1], sys.argv[2])
PY
        # Observed fingerprints identify the executed tools; they are not allowlists.
        printf '%s\t%s\t%s\n' "$tool" "$path" \
            "$(sha256sum "$path" | cut -d ' ' -f 1)" >> "$identity"
        if [ -n "${KUASAR_E2E_TOOL_OUTPUT:-}" ]; then
            mkdir -p "$KUASAR_E2E_TOOL_OUTPUT"
            install -m 0755 "$path" "$KUASAR_E2E_TOOL_OUTPUT/$tool"
        fi
    done
    emit E2E_ZOT_BIN "$zot_bin"
    emit E2E_VGW_BIN "$vgw_bin"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi
