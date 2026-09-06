[English](quickstart.md) | [简体中文](quickstart_zh.md)

# Quick Start

This guide is for first-time Kuasar Sandbox users. It starts a standalone node from one aggregate release, then uses the unmodified E2B Python SDK to build a template, create a real MicroVM, execute a command, pause the sandbox, reconnect and resume it, and finally terminate it.

This is the shortest supported experience path, not the complete release-validation procedure. For detailed networking, TLS, registry, and internal data-path setup, see the [Demo](../test/demo/DEMO.md). For all six owner suites and the platform integration gate, see the [Aggregate Release Validation Guide](../test/QUICKSTART.md).

## 1. Check the host

Current prebuilt aggregate-release assets support Linux x86_64 and require glibc 2.38 or newer, such as Ubuntu 24.04 with glibc 2.39. The host also needs:

- systemd as PID 1;
- cgroup v2;
- read/write access to `/dev/kvm`;
- root or passwordless `sudo`;
- Python 3.

The download steps use GitHub CLI and an authenticated GitHub session. Running the MicroVM does not depend on GitHub CLI. The example build sandbox requests 2 vCPUs and 6 GiB of memory, so the node also needs capacity for system services and the sandbox created from the resulting template.

On Ubuntu 24.04, install the host tools and shared libraries used by this Quick Start:

```bash
sudo apt-get update
sudo apt-get install -y \
    ca-certificates curl docker.io e2fsprogs gh iproute2 iptables \
    libgcc-s1 liblz4-1 libsnappy1v5 libstdc++6 libzstd1 openssl \
    procps python3 python3-venv sqlite3 tar util-linux zlib1g
```

Run the preflight checks:

```bash
(
set -e
test "$(uname -s)" = Linux
test "$(uname -m)" = x86_64
test "$(ps -p 1 -o comm=)" = systemd
test -f /sys/fs/cgroup/cgroup.controllers
test -c /dev/kvm
sudo -n test -r /dev/kvm
sudo -n test -w /dev/kvm
sudo -n true
glibc_version="$(getconf GNU_LIBC_VERSION | awk '{print $2}')"
test "$(printf '%s\n' 2.38 "$glibc_version" | sort -V | head -n 1)" = 2.38
for tool in gh tar sha256sum python3 openssl ip curl sqlite3 iptables mkfs.ext4 docker ldd; do
    command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done
gh auth status
sudo -n docker info >/dev/null
)
```

The block succeeds only when every check passes. Do not continue after a failed command.

Docker is used here only to prepare a local OCI registry and import a base image. It is not a runtime dependency of every Kuasar Sandbox deployment. When an existing OCI registry is reachable from the build MicroVM, configure it as described in the [Demo registry section](../test/demo/DEMO.md). Zot is needed only when `demo_prep.sh` is configured to start a local Zot registry; VersityGW is needed only for the optional `COPY` build-step demonstration.

The source Makefiles also accept `TARGET_ARCH=aarch64`, but that does not mean aarch64 is currently published as a prebuilt GitHub release asset.

## 2. Download, verify, and extract one aggregate release

The current stable aggregate release is [`release-v0.1.2`](https://github.com/kuasar-sandbox/kuasar-sandbox/releases/tag/release-v0.1.2). This guide uses the platform archive and all six release-unit archives from that exact aggregate release. Do not combine older platform scripts with newer component assets.

```bash
RELEASE_VERSION=release-v0.1.2
DOWNLOAD_DIR="$PWD/kuasar-download-$RELEASE_VERSION"
INSTALL_DIR="$PWD/kuasar-$RELEASE_VERSION"

test ! -e "$DOWNLOAD_DIR"
test ! -e "$INSTALL_DIR"
mkdir "$DOWNLOAD_DIR" "$INSTALL_DIR"
gh release view "$RELEASE_VERSION" \
    --repo kuasar-sandbox/kuasar-sandbox \
    --json tagName,isPrerelease
gh release download "$RELEASE_VERSION" \
    --repo kuasar-sandbox/kuasar-sandbox \
    --dir "$DOWNLOAD_DIR"
```

In a new directory, `gh release download` retrieves every explicit asset from the aggregate release: one platform archive, six component release-unit archives, and one `SHA256SUMS`. The internal component versions may differ, but every archive must come from the same aggregate release.

Verify the checksums before extracting all seven archives into the same directory:

```bash
(
    set -e
    cd "$DOWNLOAD_DIR"
    test "$(find . -maxdepth 1 -type f -name '*.tar.gz' | wc -l)" -eq 7
    test -f SHA256SUMS
    sha256sum --quiet -c SHA256SUMS
    for archive in ./*.tar.gz; do
        tar -xzf "$archive" -C "$INSTALL_DIR"
    done
    cd "$INSTALL_DIR"
    test -d bin && test -d deploy && test -d docs && test -d test
    grep -q 'DEMO_QUICKSTART' test/demo/demo_e2b.sh
    for executable in bin/cache-ctl bin/cloud-hypervisor; do
        ldd_output="$(ldd "$executable" 2>&1)" || { printf '%s\n' "$ldd_output" >&2; exit 1; }
        if printf '%s\n' "$ldd_output" | grep -q 'not found'; then
            printf '%s\n' "$ldd_output" >&2
            exit 1
        fi
    done
)
```

A successful `sha256sum --quiet -c` prints nothing. After extraction, `bin/` and `deploy/` are supplied by component archives, while `docs/` and `test/` come from the platform archive.

## 3. Install the unmodified E2B SDK

```bash
cd "$INSTALL_DIR"
python3 -m venv .venv
. .venv/bin/activate
python -m pip install e2b e2b-code-interpreter
python -c 'import e2b; print(e2b.__file__)'
```

Kuasar Sandbox uses the upstream Python packages. No SDK fork or local patch is required.

## 4. Prepare standalone services

The following commands start a temporary OCI registry bound only to host loopback, then reuse the release-provided `demo_prep.sh` to start the persistent store/cache services and import a base image:

```bash
sudo -n docker run -d --name kuasar-quickstart-registry \
    --network host \
    -e REGISTRY_HTTP_ADDR=127.0.0.1:5000 \
    registry:2
export DEMO_DATA_DIR="$PWD/.kuasar-quickstart"
export REGISTRY=127.0.0.1:5000
export REGISTRY_INSECURE=1
sudo -n env \
    PATH="$VIRTUAL_ENV/bin:$PATH" \
    DEMO_DATA_DIR="$DEMO_DATA_DIR" \
    REGISTRY="$REGISTRY" \
    REGISTRY_INSECURE="$REGISTRY_INSECURE" \
    bash test/demo/demo_prep.sh
```

The first run downloads and imports a base image, so its duration depends on network and storage performance. A successful preparation ends with:

```text
prep ready
```

## 5. Build a template and run a MicroVM

```bash
sudo -n env \
    PATH="$VIRTUAL_ENV/bin:$PATH" \
    DEMO_DATA_DIR="$DEMO_DATA_DIR" \
    DEMO_QUICKSTART=1 \
    bash test/demo/demo_e2b.sh
```

The script prepares standalone TLS, credentials, networking, and the E2B-compatible endpoints, then performs the following core calls through the unmodified SDK:

```python
from e2b import Sandbox, Template

template = Template().from_image("<registry>/<image>:<tag>")
build = Template.build(template, name="quickstart", cpu_count=2, memory_mb=6144)
sandbox = Sandbox.create(build.template_id, timeout=300)
sandbox_id = sandbox.sandbox_id
print(sandbox.commands.run("uname -sm").stdout)
sandbox.pause()
sandbox = Sandbox.connect(sandbox_id)  # connect auto-resumes a paused sandbox
print(sandbox.commands.run("echo resumed").stdout)
sandbox.kill()
```

The SDK does not expose a separate `resume()` method. `Sandbox.connect(sandbox_id)` reconnects to and automatically resumes the same paused logical instance. Quick Start mode also checks in-guest execution, state retention across pause/resume, and networking, then calls `kill()`. The complete Demo additionally covers template fan-out and migration.

A successful run ends with:

```text
quick start complete
```

## 6. Clean up

Whether `demo_e2b.sh` succeeds or fails, it removes the systemd units, vSwitch state, NAT rules, temporary `/etc/hosts` entries, and sandboxes owned by that run.

Stop the store/cache services while preserving their data. The temporary registry uses no persistent volume, so removing the container discards its registry data:

```bash
sudo -n env DEMO_DATA_DIR="$DEMO_DATA_DIR" bash test/demo/demo_prep.sh stop
sudo -n docker rm -f kuasar-quickstart-registry
```

Stop the services and also delete this Quick Start's store, cache, and registry-index data:

```bash
sudo -n env DEMO_DATA_DIR="$DEMO_DATA_DIR" bash test/demo/demo_prep.sh reset
sudo -n docker rm -f kuasar-quickstart-registry
```

## 7. Troubleshooting

- **`systemd is not PID1`** — run on a Linux host that boots with systemd, not in a regular container.
- **`/dev/kvm not available (rw)`** — enable hardware virtualization, load KVM, and ensure root can read and write the device.
- **`GLIBC_... not found`** — the current prebuilt assets require glibc 2.38 or newer. Use a compatible host instead of replacing the system C library manually.
- **`missing ... in bin`** — confirm that every explicit asset from the same aggregate release was downloaded and that all seven archives were extracted into one directory.
- **`e2b Python SDK not installed`** — ensure that `python3` in `PATH` comes from the virtual environment and pass the same `PATH` through `sudo env`.
- **Registry or image-pull failure** — ensure Docker can pull the base image and the registry port is free. For an external registry, provide credentials as described in the [Demo](../test/demo/DEMO.md).
- **TLS, network, or port conflict** — check TCP 443, the registry port, iptables, and local routes. See the [Demo](../test/demo/DEMO.md) for the complete data path.

## 8. Support scope and production deployment

- `release-v0.1.2` is the current non-prerelease stable aggregate version. Preview releases contain exact, verifiable component selections but remain GitHub prereleases for development and evaluation.
- Standalone mode can be used directly. Cluster mode additionally requires Registry, Router, Placer, reachable node services, shared or remotely durable data paths, and cluster networking.
- Images and snapshots can use local files, NAS/NFS or another shared filesystem, or Manifest-backed S3-compatible object storage with tiered caches.
- The base vSwitch, sandbox isolation, and the integration foundation for external policy gateways are delivered. The node-local lightweight [Egress plane](https://github.com/kuasar-sandbox/connector/issues/9) and [OpenTelemetry](https://github.com/kuasar-sandbox/kuasar-sandbox/issues/52) remain Proposed and are not prerequisites for this guide.
- Production deployments must configure production TLS, durable storage, network policy, and secure credentials. The local CA, server certificate, local registry, and demonstration credentials created by this guide are for evaluation only.

See [Deployment](deployment.md) for complete standalone and cluster topologies, [Releases](release.md) for component and aggregate version relationships, and the [Security Policy](../SECURITY.md) for private vulnerability reporting.
