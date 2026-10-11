#!/usr/bin/env bash
# Platform-owned external tools, built only from fixed owner and upstream inputs.
set -euo pipefail
[ "$#" = 2 ] || { echo 'usage: build-platform-helpers.sh ARCH OUTPUT' >&2; exit 2; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
case "$1" in x86_64) go_arch=amd64 ;; aarch64) go_arch=arm64 ;; *) exit 2 ;; esac
: "${KUASAR_WORKSPACE_ROOT:?provide the admitted owner workspace}"
: "${KUASAR_BUILD_JOBS:?provide the helper build budget}"
[[ "$KUASAR_BUILD_JOBS" =~ ^[1-9][0-9]*$ ]] || exit 2
owner="$KUASAR_WORKSPACE_ROOT/guest-runtime"
[ -d "$owner" ] && [ -f "$owner/native-deps/deps/build-versitygw.sh" ] \
    || { echo 'missing admitted guest-runtime build entry' >&2; exit 1; }
# The generic builder has verified the complete owner root and declaration.
# Never consult host helper overrides, PATH binaries or another source ref.
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work"/{bin,tarballs,tmp}
source "$ROOT/ci/hosted/bootstrap.sh"
digest=6a667e38d78a08effdf0b7c3c3e4b9929233b333c8a53c16b62eded7d43eeedc
archive="$work/tarballs/versitygw-1.5.0.tar.gz"
download https://codeload.github.com/versity/versitygw/tar.gz/refs/tags/v1.5.0 "$archive" "$digest"
bash "$ROOT/ci/integration/materialize-source.sh" "$archive" "$work/versitygw"
touch "$work/versitygw/.extracted"
BINDIR="$work/bin" BUILD_DIR="$work/build" TARBALL_CACHE="$work/tarballs" TMPDIR="$work/tmp" \
    VERSITYGW_SRC="$work/versitygw" VERSITYGW_TARBALL="$archive" VERSITYGW_TARBALL_SHA256="$digest" \
    VERSITYGW_GOFLAGS="-mod=mod -p=$KUASAR_BUILD_JOBS" GOMAXPROCS="$KUASAR_BUILD_JOBS" GO_ARCH="$go_arch" \
    bash "$owner/native-deps/deps/build-versitygw.sh"
# The upstream owner recipe disables GOWORK for its independent Go module.
asset="zot-linux-$go_arch-minimal"
url=https://github.com/project-zot/zot/releases/download/v2.1.17
curl --fail --location --silent --show-error --connect-timeout 20 --max-time 600 \
    "$url/checksums.sha256.txt" -o "$work/checksums"
expected="$(awk -v name="$asset" '{entry=$2; sub(/^\*/, "", entry); if (entry == name) print $1}' "$work/checksums")"
download "$url/$asset" "$work/bin/zot" "$expected"
install -m 0755 "$work/bin/zot" "$2/zot"
install -m 0755 "$work/bin/versitygw" "$2/versitygw"
