#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ORG="$(cd "$ROOT/.." && pwd)"
TARGET_ARCH="${TARGET_ARCH:-$(uname -m)}"
case "$TARGET_ARCH" in amd64) TARGET_ARCH=x86_64 ;; arm64) TARGET_ARCH=aarch64 ;; esac
HOST_ARCH="$(uname -m)"
case "$HOST_ARCH" in amd64) HOST_ARCH=x86_64 ;; arm64) HOST_ARCH=aarch64 ;; esac
die() { echo "source-owner: $*" >&2; exit 2; }
required_bins() { case "$1" in
connector) echo "connector-ctl" ;;
guest-runtime) echo "mkfs.erofs store-ctl flatten-ctl" ;;
accelerator) echo "mkfs.erofs manifest-ctl store-ctl cache-ctl flatten-ctl" ;;
sandboxer) echo "mkfs.erofs vmlinux manifest-ctl store-ctl cache-ctl flatten-ctl sandbox-ctl sandbox-init cloud-hypervisor sandbox-runtime.bundle" ;;
orchestrator|kuasar-sandbox) awk '!/^($|#)/ {print $2}' "$ROOT/release/bin-inputs.manifest" | xargs ;;
*) die "unknown owner $1" ;;
esac; }
native_components() { case "$1" in connector) ;; guest-runtime) echo erofs ;; accelerator) echo "erofs rocksdb" ;; sandboxer|orchestrator|kuasar-sandbox) echo "vmlinux erofs envd rocksdb cloud-hypervisor" ;; *) die "unknown owner $1" ;; esac; }
images() { case "$1" in connector|guest-runtime) ;; accelerator) echo "python:3.12-slim python:3.12-alpine" ;; sandboxer) echo "python:3.12-slim busybox:latest" ;; orchestrator|kuasar-sandbox) echo "python:3.12-slim" ;; *) die "unknown owner $1" ;; esac; }
needs_uffd() { case "$1" in sandboxer|kuasar-sandbox) return 0 ;; *) return 1 ;; esac; }
needs_working_set() { case "$1" in sandboxer|kuasar-sandbox) return 0 ;; *) return 1 ;; esac; }
copy_required_bins() {
local owner=$1 name repo src
rm -rf "$ROOT/bin/$TARGET_ARCH"; mkdir -p "$ROOT/bin/$TARGET_ARCH"
find "$ROOT/bin" -maxdepth 1 -type l -delete 2>/dev/null || true
for name in $(required_bins "$owner"); do
repo="$(awk -v n="$name" '!/^($|#)/ && $2==n {print $1; exit}' "$ROOT/release/bin-inputs.manifest")"
[ -n "$repo" ] || die "no bin-inputs entry for $name"
src="$ORG/$repo/bin/$TARGET_ARCH/$name"
[ -f "$src" ] || die "$owner closure did not build $repo/bin/$TARGET_ARCH/$name"
cp -f "$src" "$ROOT/bin/$TARGET_ARCH/$name"
if [ "$HOST_ARCH" = "$TARGET_ARCH" ]; then ln -sfn "$TARGET_ARCH/$name" "$ROOT/bin/$name"; fi
done
}
build_owner() { local owner=$1; case "$owner" in
connector) make -C "$ORG/connector" build ;;
guest-runtime) make -C "$ORG/accelerator" store-ctl; make -C "$ORG/guest-runtime" flatten-ctl; make -C "$ROOT" e2e-zot ;;
accelerator) make -C "$ORG/accelerator" build; make -C "$ORG/guest-runtime" flatten-ctl ;;
sandboxer) make -C "$ORG/accelerator" build; make -C "$ORG/sandboxer" build; make -C "$ORG/guest-runtime" build ;;
orchestrator|kuasar-sandbox) make -C "$ROOT" build; make -C "$ROOT" e2e-tools ;;
*) die "unknown owner $owner" ;;
esac; copy_required_bins "$owner"; make -C "$ROOT" assemble-e2e; }
[ "$#" -ge 2 ] || die "usage: source-owner.sh <native-components|images|required-bins|needs-uffd|needs-working-set|build> <owner>"
cmd=$1 owner=$2
case "$cmd" in native-components) native_components "$owner" ;; images) images "$owner" ;; required-bins) required_bins "$owner" ;; needs-uffd) needs_uffd "$owner" ;; needs-working-set) needs_working_set "$owner" ;; build) build_owner "$owner" ;; *) die "unknown command $cmd" ;; esac
