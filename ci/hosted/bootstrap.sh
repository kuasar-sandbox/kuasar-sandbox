#!/usr/bin/env bash
# Trusted standard Ubuntu prerequisites. Never source this from a requested candidate.
set -euo pipefail

EROFS_VERSION=1.9.1
EROFS_SHA256=a9ef5ab67c4b8d2d3e9ed71f39cd008bda653142a720d8a395a36f1110d0c432

die() { echo "hosted-bootstrap: $*" >&2; exit 1; }
need() { local tool; for tool in "$@"; do command -v "$tool" >/dev/null || die "missing required tool: $tool"; done; }
verify_sha256() {
    [[ "$2" =~ ^[0-9a-f]{64}$ ]] || die "invalid SHA256 pin"
    printf '%s  %s\n' "$2" "$1" | sha256sum --check --status || die "checksum mismatch: $1"
}
download() {
    local url=$1 output=$2 digest=$3
    [[ "$url" = https://* && "$digest" =~ ^[0-9a-f]{64}$ ]] || die "download needs HTTPS and a SHA256 pin"
    curl --fail --location --silent --show-error --retry 4 --retry-all-errors \
        --connect-timeout 20 --max-time 600 "$url" -o "$output"
    verify_sha256 "$output" "$digest"
}

select_profile() {
    profile=$1
    with_vm=false
    # Include packages even when the standard image currently preinstalls them.
    packages=(ca-certificates curl git jq python3 python3-yaml tar gzip xz-utils unzip
        coreutils findutils gawk sed grep diffutils util-linux file time binutils)
    case "$profile" in
        control) ;;
        release-control) ;;
        artifact-arm) ;;
        artifact-prepare) packages+=(python3-pip) ;;
        artifact-x86) with_vm=true ;;
        *) die "unknown profile: $profile" ;;
    esac
    if $with_vm || [ "$profile" = artifact-arm ]; then
        packages+=(python3-venv python3-pip iproute2 iptables nftables kmod acl
            e2fsprogs procps psmisc socat redis-server rsync cpio zstd lz4
            systemd udev dbus iputils-ping netcat-openbsd openssl sqlite3 zip strace)
    fi
}

emit() { export "$1=$2"; printf '%s=%s\n' "$1" "$2" >> "$GITHUB_ENV"; }
add_path() { export PATH="$1:$PATH"; printf '%s\n' "$1" >> "$GITHUB_PATH"; }

resource_budget() {
    # Affinity also bounds existing native recipes that explicitly use nproc.
    # Leave 2 GiB for the OS, then budget 2 GiB per compiler job. Do not set
    # MAKEFLAGS: parallel top-level build/assemble goals would race each other.
    python3 - <<'PY'
import os
from pathlib import Path

cpus = sorted(os.sched_getaffinity(0))
memory = int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines()
                  if line.startswith('MemAvailable:'))) * 1024
limit = Path('/sys/fs/cgroup/memory.max')
if limit.exists() and limit.read_text().strip() != 'max':
    memory = min(memory, int(limit.read_text()))
jobs = min(len(cpus), max(1, (memory - 2 * 1024**3) // (2 * 1024**3)))
print(jobs, ','.join(map(str, cpus[:jobs])))
PY
}

install_readers() (
    # Called explicitly inside Workbench. Guest static build flags and link maps stay
    # owned by guest-runtime/native-deps/deps/build-erofs.sh.
    local archive="$KUASAR_HOSTED_ROOT/erofs-readers.tar.gz"
    local source="$KUASAR_HOSTED_ROOT/erofs-readers"
    download "https://codeload.github.com/erofs/erofs-utils/tar.gz/refs/tags/v${EROFS_VERSION}" "$archive" "$EROFS_SHA256"
    mkdir -p "$source"
    tar -xzf "$archive" --strip-components=1 -C "$source"
    cd "$source"
    ./autogen.sh
    ./configure --disable-lz4 --disable-lzma --without-zlib \
        --without-libzstd --without-libdeflate --without-xxhash \
        --without-libcurl --without-openssl --without-libxml2 \
        --without-json-c --without-libnl3 --disable-multithreading
    make -C lib -j"$KUASAR_BUILD_JOBS"
    make -C mkfs -j"$KUASAR_BUILD_JOBS"
    make -C fsck -j"$KUASAR_BUILD_JOBS"
    make -C dump -j"$KUASAR_BUILD_JOBS"
    install -m 0755 mkfs/mkfs.erofs fsck/fsck.erofs dump/dump.erofs "$KUASAR_HOSTED_ROOT/bin/"
    install -m 0644 COPYING "$KUASAR_HOSTED_ROOT/erofs-readers.COPYING"
    "$KUASAR_HOSTED_ROOT/bin/mkfs.erofs" --version
    local fsck_help dump_help
    fsck_help=$("$KUASAR_HOSTED_ROOT/bin/fsck.erofs" --help 2>&1)
    dump_help=$("$KUASAR_HOSTED_ROOT/bin/dump.erofs" --help 2>&1)
    grep -Fq -- --extract <<< "$fsck_help" || die "fsck.erofs lacks --extract"
    grep -Fq -- --path <<< "$dump_help" || die "dump.erofs lacks --path"
    grep -Fq -- --cat <<< "$dump_help" || die "dump.erofs lacks --cat"
)

install_runtime_reader() {
    # Reuse the existing release verifier at a reviewed immutable source, not a
    # candidate script or an executable from the runtime image.
    local revision=4c3999beb54f67cf0990b98c1709cf1a199ff08b
    local verifier="$KUASAR_HOSTED_ROOT/bin/runtime-payloads.py"
    download "https://raw.githubusercontent.com/kuasar-sandbox/guest-runtime/$revision/scripts/release-runtime-payloads.py" \
        "$verifier" 01d97e1cc7aed550305e13b4dfd1daa95a3740be84512d195aed46a277e661fd
    emit KUASAR_RUNTIME_READER "$verifier"
}

render_kvm_rule() {
    [ "$#" -eq 2 ] || die "KVM rule requires job uid and primary gid"
    if [ "${GITHUB_ACTIONS:-}" != true ] || [ "${RUNNER_ENVIRONMENT:-}" != github-hosted ] \
        || [ "${RUNNER_OS:-}" != Linux ] || [ "${RUNNER_ARCH:-}" != X64 ]; then
        die "KVM rule requires a disposable GitHub-hosted Linux x64 job"
    fi
    local value
    for value in "$@"; do
        if ! [[ "$value" =~ ^[1-9][0-9]{0,9}$ ]] || [ "$value" -gt 4294967294 ]; then
            die "KVM rule requires non-root numeric job uid/gid"
        fi
    done
    printf 'SUBSYSTEM=="misc", KERNEL=="kvm", GROUP:="%s", MODE:="0660"\n' "$2"
}

configure_vm() {
    local job_uid job_gid kvm_rule
    job_uid=$(id -u)
    job_gid=$(id -g)
    kvm_rule=$(render_kvm_rule "$job_uid" "$job_gid")
    # Use the environment's Docker tools. Missing capabilities fail
    # without selecting another installation or requiring a toolchain manager.
    need docker systemctl ip modprobe setfacl mkfs.ext4 udevadm
    docker info >/dev/null
    [ "$(stat -fc %T /sys/fs/cgroup)" = cgroup2fs ] || die "cgroup v2 is required"
    [ -d /run/systemd/system ] || die "systemd is required"
    sudo -n modprobe tun vhost_vsock
    sudo -n sysctl -w vm.unprivileged_userfaultfd=1
    local device
    for device in /dev/kvm /dev/vhost-vsock /dev/net/tun; do
        [ -c "$device" ] || die "required VM device missing: $device"
        # Keep the existing per-job ACLs on vhost-vsock and TUN.
        sudo -n setfacl -m "u:$job_uid:rw" "$device"
        if [ ! -r "$device" ] || [ ! -w "$device" ]; then die "VM device inaccessible: $device"; fi
    done
    # Headless uaccess events can remove KVM's named ACL on the same inode.
    # The current primary group survives that loss; no membership/world changes.
    printf '%s\n' "$kvm_rule" > "$KUASAR_HOSTED_ROOT/99-kuasar-job-kvm.rules"
    sudo -n install -o root -g root -m 0644 "$KUASAR_HOSTED_ROOT/99-kuasar-job-kvm.rules" \
        /etc/udev/rules.d/99-kuasar-job-kvm.rules
    sudo -n udevadm control --reload-rules
    sudo -n udevadm trigger --action=change --subsystem-match=misc --sysname-match=kvm
    sudo -n udevadm settle --timeout=30
    sudo -n chgrp "$job_gid" /dev/kvm
    sudo -n chmod 0660 /dev/kvm
    sudo -n setfacl -x "u:$job_uid" /dev/kvm
    # Replay the observed ACL loss and verify open as the job user, before sudo.
    python3 - <<'PY'
import os

fd = os.open('/dev/kvm', os.O_RDWR | os.O_CLOEXEC)
os.close(fd)
PY
    # Check the unprivileged syscall, not just the configured sysctl value.
    python3 - <<'PY'
import ctypes
import os

libc = ctypes.CDLL(None, use_errno=True)
fd = libc.syscall(323, os.O_CLOEXEC | os.O_NONBLOCK)  # x86_64 userfaultfd
if fd < 0:
    raise OSError(ctypes.get_errno(), 'userfaultfd is required')
os.close(fd)
PY
    emit PIP_INDEX_URL https://pypi.org/simple
}


main() {
    if [ "$#" -ne 2 ] || [ "$1" != --profile ]; then
        die "usage: bootstrap.sh --profile control|release-control|artifact-prepare|artifact-x86|artifact-arm"
    fi
    select_profile "$2"
    case "$profile:$(uname -m)" in
        artifact-arm:aarch64|artifact-prepare:aarch64) ;;
        artifact-arm:*) die "artifact-arm requires a native ARM64 job" ;;
        *:x86_64) ;;
        *) die "this control/execution profile requires x86_64" ;;
    esac
    # shellcheck disable=SC1091
    . /etc/os-release
    if [ "$ID" != ubuntu ] || [ "$(uname -s)" != Linux ]; then die "Ubuntu Linux is required"; fi
    if $with_vm; then render_kvm_rule "$(id -u)" "$(id -g)" >/dev/null; fi
    : "${RUNNER_TEMP:?}" "${GITHUB_ENV:?}" "${GITHUB_PATH:?}"
    need sudo curl sha256sum tar python3
    sudo -n apt-get update
    sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${packages[@]}"
    need git jq python3 flock file /usr/bin/time
    emit KUASAR_HOSTED_ROOT "$(mktemp -d "$RUNNER_TEMP/kuasar-hosted.XXXXXX")"
    mkdir -p "$KUASAR_HOSTED_ROOT/bin" "$KUASAR_HOSTED_ROOT/tarballs" "$KUASAR_HOSTED_ROOT/sources"
    emit KUASAR_SOURCE_CACHE_ROOT "$KUASAR_HOSTED_ROOT/sources"
    emit KUASAR_NATIVE_CACHE_ROOT "$KUASAR_HOSTED_ROOT/native"
    emit KUASAR_TARBALL_CACHE "$KUASAR_HOSTED_ROOT/tarballs"
    emit TARBALL_CACHE "$KUASAR_HOSTED_ROOT/tarballs"
    local jobs cpus
    read -r jobs cpus < <(resource_budget)
    [[ "$jobs" =~ ^[1-9][0-9]*$ && "$cpus" =~ ^[0-9]+(,[0-9]+)*$ ]] || die "cannot bound build parallelism"
    emit KUASAR_BUILD_JOBS "$jobs"
    emit KUASAR_BUILD_CPUS "$cpus"
    emit GOMAXPROCS "$jobs"
    emit CARGO_BUILD_JOBS "$jobs"
    add_path "$KUASAR_HOSTED_ROOT/bin"
    if [ "$profile" = artifact-arm ]; then need docker; docker info >/dev/null; fi
    if $with_vm; then configure_vm; fi
    echo "hosted-bootstrap: profile=$profile jobs=$jobs cpus=$cpus"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi
