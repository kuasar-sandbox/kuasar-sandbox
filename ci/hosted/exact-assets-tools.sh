#!/usr/bin/env bash
# Trusted host TEST tools only; never writes into the extracted product package.
set -euo pipefail

tooling_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=ci/hosted/bootstrap.sh
source "$tooling_dir/bootstrap.sh"

GUEST_TOOLS_COMMIT=494dbceae683d6b20cdbec00fe6b1f554ea2f508
VERSITYGW_SOURCE_SHA256=6a667e38d78a08effdf0b7c3c3e4b9929233b333c8a53c16b62eded7d43eeedc
ZOT_BINARY_SHA256=523e5bf29a013db09115f780c3152af98fc5b65fc408a0d3e6c293643dc9bde7

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

main() {
    [ "$#" -eq 0 ] || die "usage: exact-assets-tools.sh (no arguments)"
    : "${RUNNER_TEMP:?}" "${GITHUB_ENV:?}" "${KUASAR_CI_DIR:?}"
    : "${KUASAR_BUILD_JOBS:?}" "${KUASAR_BUILD_CPUS:?}"
    [[ "$RUNNER_TEMP" = /* && "$RUNNER_TEMP" != *$'\n'* && -d "$RUNNER_TEMP" ]] \
        || die "RUNNER_TEMP must be an absolute existing directory"
    [[ "$KUASAR_BUILD_JOBS" =~ ^[1-9][0-9]*$ && "$KUASAR_BUILD_CPUS" =~ ^[0-9]+(,[0-9]+)*$ ]] \
        || die "invalid host tool build budget"
    local tools_root identity
    tools_root=$(mktemp -d "$RUNNER_TEMP/kuasar-exact-tools.XXXXXX")
    identity="$KUASAR_CI_DIR/host-tools.tsv"
    mkdir -p "$tools_root"/{bin,recipes,tarballs,tmp} "$KUASAR_CI_DIR"
    printf 'input\tidentity\tsha256\n' > "$identity"
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
        GOTOOLCHAIN=local GOMAXPROCS="$KUASAR_BUILD_JOBS" GO_ARCH=amd64 \
        taskset -c "$KUASAR_BUILD_CPUS" bash "$tools_root/recipes/build-versitygw.sh"

    BINDIR="$tools_root/bin" TARGET_ARCH=x86_64 TARBALL_CACHE="$tools_root/tarballs" \
        ZOT_VERSION=v2.1.17 \
        ZOT_URL=https://github.com/project-zot/zot/releases/download/v2.1.17/zot-linux-amd64-minimal \
        ZOT_SHA256_URL=https://github.com/project-zot/zot/releases/download/v2.1.17/checksums.sha256.txt \
        bash "$tooling_dir/../integration/ensure-zot.sh"
    verify_sha256 "$tools_root/bin/zot" "$ZOT_BINARY_SHA256"
    printf 'zot-release\tv2.1.17\t%s\n' "$ZOT_BINARY_SHA256" >> "$identity"
    local tool
    for tool in zot versitygw; do
        [ -x "$tools_root/bin/$tool" ] || die "missing host test tool: $tool"
        printf '%s\t%s\t%s\n' "$tool" "$tools_root/bin/$tool" \
            "$(sha256sum "$tools_root/bin/$tool" | cut -d ' ' -f 1)" >> "$identity"
    done
    emit E2E_ZOT_BIN "$tools_root/bin/zot"
    emit E2E_VGW_BIN "$tools_root/bin/versitygw"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi
