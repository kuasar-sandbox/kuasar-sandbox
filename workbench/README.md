[English](README.md) | [简体中文](README_zh.md)

# Workbench

Workbench supplies the native Linux build tools and system environment for a
selected Kuasar Sandbox release. One image supports ordinary UID builds and a
systemd PID 1 environment with private Docker/containerd. It contains external
test image archives, while products, test helpers and Demo wheels come from the
matching release. See the [public E2E guide](../test/README.md) for selection
and the [Demo guide](../test/demo/DEMO.md) for the complete example.

## Host and image

Use native x86_64 or aarch64 Linux with Docker Engine and Python 3. The launcher
uses the local Unix Docker endpoint. Docker Desktop, remote Docker endpoints,
architecture emulation, Podman and runner registration are outside V1.

System mode is a trusted administrator tool. It uses rootful Docker with
`--privileged`, private namespaces and its own daemon data. Host requirements
are native Linux, Docker Engine, Python 3, cgroup v2 with cpu/memory/pids
controllers for the requested budgets, and storage suitable for the image's
inner overlay2 daemon. No host AppArmor parser, profile loader, custom seccomp
policy or alternate Docker build is required. Docker access already grants
administrator-level authority; do not expose this environment to untrusted code.

Ordinary UID builds need a writable task checkout and state directory, without
KVM or added system privileges. Generic system startup does not require KVM,
4096-byte pages, userfaultfd or BPF. It checks systemd/private-daemon readiness
and reports actual capability availability. Selected product tests still require
their real devices and kernel features; unavailable capability is not a pass.
Host profiles, global sysctls, modules and existing services are not managed.

Acquire the selected release's image through its registry tag or verify its
`SHA256SUMS` entry and import its native `workbench-<arch>-v*.tar.gz` with
`docker load`. This is a gzip-compressed Docker image archive; never extract it
into the product release tree. The registry tag is
`ghcr.io/kuasar-sandbox/workbench:vX.Y.Z[-preview.YYYYMMDD[.N]]`, using the
aggregate version without the `release-` prefix. New `workbench-v1` releases
require both native image archives; historical releases keep their original assets.

## Build with an ordinary UID

Prepare your own six-repository checkout using the existing
[build entry](../Makefile). Place instance state outside that checkout. Set
`IMAGE` to the acquired image tag or ID, `SOURCES` to the parent of those
checkouts, and `STATE` to a new directory owned by your UID:

```sh
python3 workbench/workbench check --image "$IMAGE" --mode build
python3 workbench/workbench --root "$STATE" --name build start \
  --image "$IMAGE" --mode build --source "$SOURCES" --cpus 2 --memory-gib 8
python3 workbench/workbench --root "$STATE" --name build exec -- \
  make -C /src/kuasar-sandbox build
python3 workbench/workbench --root "$STATE" --name build stop
```

This mode uses your UID/GID, drops every capability, has no extra devices and
uses a read-only container root. It does not chown the checkout. Build outputs
follow the existing Makefiles; `/build`, `/output` and `/work` are also writable
private mounts. Go/Cargo caches and HOME belong to the instance. The image
provides Go, Rust/Cargo, C/C++, clang/LLVM, static and dynamic development
libraries and kernel prerequisites. Future source revisions may still download
modules, crates or native dependency sources; arbitrary source builds are not
promised to be offline.

## Prepare and run release tests

Assemble the selected original product archives and platform-release into a
separate release directory using the release instructions. Set `RELEASE` to
that directory and `ARCH` to `x86_64` or `aarch64` as appropriate:

```sh
python3 workbench/workbench --root "$STATE" --name e2e start \
  --image "$IMAGE" --inputs "$RELEASE" --cpus 2 --memory-gib 8 --network none
python3 workbench/workbench --root "$STATE" --name e2e exec -- \
  python3 -B /inputs/release/test/e2e/e2e prepare \
  --release-dir /inputs/release --workdir /work/prepared --arch "$ARCH" \
  --suite image --suite storage --exclude storage.obs.sh \
  --deps-dir /opt/workbench/deps --offline
python3 workbench/workbench --root "$STATE" --name e2e exec -- \
  python3 -B /work/prepared/test/e2e/e2e run --workdir /work/prepared \
  --arch "$ARCH" --suite image --suite storage --exclude storage.obs.sh
```

These suites are a minimal native ARM example. Hosted ARM release CI checks artifact bytes and image import only because it has no KVM device; this is not system/offline acceptance. Separate native KVM acceptance with the published image also runs
`sandbox.lifecycle.sh`, `snapshot.restore.sh` and `network.tapfd.sh` on ARM; add
those three `--include` options to both commands to exercise that scope. On x86_64 use the
current full ordinary selection, `--all --exclude storage.obs.sh`, for both
prepare and run. For that full selection, start with `--network bridge`: `--offline` still blocks dependency fetches during prepare, while the existing Demo requires real Internet egress during run. Release qualification additionally disconnects the owned bridge during prepare and restores it only afterward. Do not remove case architecture guards. Credentialed OBS
remains an explicit choice requiring its own network and credentials.

`--network none` removes external networking from the outer instance; private
Guest, Registry, Store and Proxy traffic remains available. The image defaults
to `E2E_DEPS_DIR=/opt/workbench/deps` and `E2E_OFFLINE=1`. For an intentional
online prepare in an instance started with `--network bridge`, invoke
`exec -- env E2E_OFFLINE=0 python3 ...` without `--offline`. Pass only the task
variables needed by that command. Offline mode controls acquisition, not case
selection. Missing or corrupt required inputs fail; helpers and locked wheels
are always consumed from the selected platform-release, without PATH or global
Python package fallback.

## Lifecycle and isolation

`exec` without a command opens a shell. `stop` asks systemd to stop private
services and waits up to 60 seconds; an unsuccessful graceful stop is an error.
`start` on a stopped instance uses its recorded container, mounts and budgets
and requires the original image and mode. Use a fresh name to change those
settings. `cleanup` stops and removes only the recorded container, network and
daemon data; it preserves work, build, home, journal and output directories.
Add `--delete-output` explicitly to delete those directories too. Ownership
records remain as evidence; use a new instance name after cleanup. An older
instance with recorded external policy state must be cleaned with its original
launcher before switching versions; the new launcher does not alter that policy.

Each system instance retains private PID/UTS/IPC/mount/network/cgroup namespaces,
bpffs, Docker/containerd sockets and data, and read-only release inputs. It does
not use host namespaces, a host Docker socket or a host-root bind mount.
System mode has administrator capabilities and host-device access through
Docker privileged mode; these private resources avoid operational collisions,
not a hostile-root security boundary. The nofile limit is 1048576 for the outer
container and daemon units so nested services can use their required limits.

The entrypoint creates read-only private views of host-global sysfs/sysctl paths
and disables container-inappropriate udev/module-loading units. A private bind
masks inner Docker's AppArmor detection input so it does not load host policies.
No host policy is added, replaced or removed. These are precautions against
accidental service startup effects, not restrictions against a malicious admin.

The image sets `DefaultTasksMax=infinity` for internal systemd units. The outer
`--pids-limit` remains the environment-wide bound, and explicitly configured
per-unit limits still apply. This avoids an implicit per-service ceiling that
can prevent nested services from starting.

CPU quota, memory and process budgets remain. Instances are not automatically
pinned to the same first N host CPUs. Capacity checks are not disk quotas; host
kernel, disk and NIC contention still exists. Use trusted administrator tasks,
not untrusted multi-tenancy or claims of independent performance measurements.

## Maintainer build and verification

`collect-inputs.py --cases <assembled-case-directory> --arch x86_64 --arch
aarch64 --output <new-directory>` uses the public runner's discovery and shared
external-image request function. It resolves each tag once, persists the exact
registry responses for retries, downloads with skopeo and verifies archive
layers/config against those responses. No daemon cache or credentials enter
the image. Corrupt existing collection files fail instead of being repaired.

On each native architecture, `build.py --version release-vX.Y.Z --cases
<assembled-case-directory> --deps-dir <collection> --output <stage>` builds
committed inputs, saves the canonical image once and writes a receipt with
image ID, archive hash/size, source identity and all context files. The pinned
Ubuntu base and official Go/Rust archive checksums are in
[Dockerfile](Dockerfile) and [toolchains.json](toolchains.json); installed system
package versions remain in `/usr/share/workbench/packages.tsv`.

Run `python3 -B workbench/test_workbench.py` for unit contracts. Run
`python3 -B workbench/test-system.py --image "$IMAGE" --root <new-task-path>`
on a suitable native host for real two-instance isolation, daemon failure,
restart, timeout, startup failure and collision checks. Add
`--protect-container <existing-name>` to record an existing service's unchanged
identity and start time. Results and retained output stay in that task path.
These checks supplement native full builds and the current public E2E cases;
they do not replace compiler-free release acceptance.

`test-system.py --smoke --image "$IMAGE" --root <new-task-path>` verifies two
administrator instances with real inner containers, identical names/ports,
private daemons and owned cleanup. Its tiny runtime fixture uses tools already
inside the workbench; it needs no downloaded test image or KVM. Native CI runs
this functional check instead of a host security-policy test. Full system E2E,
actual KVM/UFFD/TUN/BPF checks and compiler-free acceptance remain separate and
must pass for the declared product scope.

Installed tools retain their upstream license and source records. The former
custom policy sources and their unused vendor copies are no longer shipped.
