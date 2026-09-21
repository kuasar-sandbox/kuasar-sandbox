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
    with_go=false with_native=false with_kernel=false with_readers=false with_vm=false with_cross=false
    # Include packages even when the standard image currently preinstalls them.
    packages=(ca-certificates curl git jq python3 python3-yaml tar gzip xz-utils unzip
        coreutils findutils gawk sed grep diffutils util-linux file time binutils)
    case "$profile" in
        control) ;;
        release-control) with_go=true ;;
        kernel) with_go=true; with_kernel=true ;;
        runtime|runtime-publish) with_go=true; with_native=true; with_readers=true ;;
        source) with_go=true; with_native=true; with_kernel=true; with_readers=true; with_vm=true ;;
        exact-assets) with_go=true; with_readers=true; with_vm=true ;;
        artifact-build) with_go=true; with_native=true; with_kernel=true; with_readers=true ;;
        artifact-cross) with_go=true; with_native=true; with_kernel=true; with_readers=true; with_cross=true ;;
        artifact-arm) with_readers=true ;;
        artifact-prepare) with_readers=true ;;
        artifact-x86) with_readers=true; with_vm=true ;;
        *) die "unknown profile: $profile" ;;
    esac
    if $with_native || $with_kernel || $with_readers; then
        packages+=(build-essential pkg-config)
    fi
    if $with_kernel; then
        packages+=(bc bison flex libelf-dev libssl-dev libncurses-dev)
    fi
    if $with_native || $with_readers; then
        packages+=(autoconf automake libtool uuid-dev)
    fi
    if $with_native; then
        # Retain the native crypto prerequisites already merged in #126.
        packages+=(patch libgcrypt20-dev libgpg-error-dev libssl-dev liblz4-dev libzstd-dev zlib1g-dev libfuse3-dev)
    fi
    if [[ "$profile" = source || "$profile" = artifact-build || "$profile" = artifact-cross ]]; then
        packages+=(cmake clang llvm libclang-dev libsnappy-dev libssl-dev)
    fi
    if $with_cross; then
        packages+=(gcc-aarch64-linux-gnu g++-aarch64-linux-gnu binutils-aarch64-linux-gnu
            libc6-dev-arm64-cross uuid-dev:arm64 libgcrypt20-dev:arm64 libgpg-error-dev:arm64)
    fi
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

configure_go() {
    need go
    # The environment owns compiler selection, including GOROOT/GOTOOLCHAIN.
    go version
    emit GOPROXY https://proxy.golang.org,direct
    emit GOSUMDB sum.golang.org
    emit GOPATH "$KUASAR_HOSTED_ROOT/gopath"
    emit GOCACHE "$KUASAR_HOSTED_ROOT/go-cache"
    emit GOMODCACHE "$KUASAR_HOSTED_ROOT/go-modules"
}

install_readers() (
    # Host validation tools only. Guest static build flags and link maps stay
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

configure_cross() {
    need aarch64-linux-gnu-gcc aarch64-linux-gnu-g++ cargo rustc
    local target=aarch64-unknown-linux-gnu libdir
    libdir=$(rustc --print target-libdir --target "$target")
    if ! compgen -G "$libdir/libstd-*.rlib" >/dev/null; then
        # Install only the matching standard library into the provided toolchain.
        # Do not select or upgrade the compiler.
        need rustup
        rustup target add "$target"
    fi
    compgen -G "$libdir/libstd-*.rlib" >/dev/null || die "provided Rust toolchain lacks $target std"
    # Recipe-scoped CC/pkg-config selection is owned by the existing Makefiles.
    # In particular never export target GOARCH or PKG_CONFIG_LIBDIR here.
}

configure_cross_apt() {
    # Standard Ubuntu uses separate amd64 and arm64 archive endpoints. Restrict
    # the existing deb822 sources before adding the target-only ports source.
    [ "${RUNNER_ENVIRONMENT:-}" = github-hosted ] || die "cross packages require a disposable hosted job"
    sudo -n env KUASAR_APT_SOURCE=/etc/apt/sources.list.d/ubuntu.sources python3 - <<'PY'
import os
from pathlib import Path
source = Path(os.environ['KUASAR_APT_SOURCE'])
text = source.read_text()
lines = []
skip_continuation = False
types = 0
for line in text.splitlines():
    if line.startswith((' ', '\t')):
        if not skip_continuation:
            lines.append(line)
        continue
    skip_continuation = False
    if line.lower().startswith('architectures:'):
        skip_continuation = True
        continue
    lines.append(line)
    if line.lower().startswith('types:'):
        lines.append('Architectures: amd64')
        types += 1
if not types:
    raise SystemExit('ubuntu.sources contains no deb822 Types field')
source.write_text('\n'.join(lines) + '\n')
PY
    sudo -n dpkg --add-architecture arm64
    printf 'Types: deb\nURIs: http://ports.ubuntu.com/ubuntu-ports\nSuites: %s %s-updates %s-security\nComponents: main universe restricted multiverse\nArchitectures: arm64\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n' \
        "$VERSION_CODENAME" "$VERSION_CODENAME" "$VERSION_CODENAME" \
        | sudo -n tee /etc/apt/sources.list.d/kuasar-arm64.sources >/dev/null
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
    # Use the environment's Docker and Rust tools. Missing capabilities fail
    # without selecting another installation or requiring a toolchain manager.
    need docker systemctl ip modprobe setfacl mkfs.ext4 udevadm
    docker info >/dev/null
    if [ "$profile" = source ]; then
        need cargo rustc
        cargo --version
        rustc --version
    fi
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
    emit CARGO_HOME "$KUASAR_HOSTED_ROOT/cargo"
    emit RUSTUP_DIST_SERVER https://static.rust-lang.org
    emit RUSTUP_UPDATE_ROOT https://static.rust-lang.org/rustup
    emit CARGO_REGISTRIES_CRATES_IO_PROTOCOL sparse
    emit CARGO_NET_GIT_FETCH_WITH_CLI true
    emit PIP_INDEX_URL https://pypi.org/simple
}


main() {
    if [ "$#" -ne 2 ] || [ "$1" != --profile ]; then
        die "usage: bootstrap.sh --profile control|release-control|kernel|runtime|runtime-publish|source|exact-assets|artifact-build|artifact-cross|artifact-prepare|artifact-x86|artifact-arm"
    fi
    select_profile "$2"
    case "$profile:$(uname -m)" in
        artifact-arm:aarch64) ;;
        artifact-arm:*) die "artifact-arm requires a native ARM64 job" ;;
        *:x86_64) ;;
        *) die "this build/control profile requires x86_64" ;;
    esac
    # shellcheck disable=SC1091
    . /etc/os-release
    if [ "$ID" != ubuntu ] || [ "$(uname -s)" != Linux ]; then die "Ubuntu Linux is required"; fi
    if $with_vm; then render_kvm_rule "$(id -u)" "$(id -g)" >/dev/null; fi
    : "${RUNNER_TEMP:?}" "${GITHUB_ENV:?}" "${GITHUB_PATH:?}"
    need sudo curl sha256sum tar python3
    if $with_cross; then configure_cross_apt; fi
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
    if $with_go; then configure_go; fi
    if $with_readers; then install_readers; install_runtime_reader; fi
    if $with_cross; then configure_cross; fi
    if [ "$profile" = artifact-arm ]; then need docker; docker info >/dev/null; fi
    if $with_vm; then configure_vm; fi
    echo "hosted-bootstrap: profile=$profile jobs=$jobs cpus=$cpus"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi
