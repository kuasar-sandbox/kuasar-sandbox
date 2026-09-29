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

System mode requires rootful Docker, cgroup v2 with cpu/cpuset/memory/pids/io
delegation, 4096-byte host pages, KVM and TUN devices, userfaultfd, BPF and
overlay2. Build mode needs none of the KVM or additional capability grants.
An invoking ordinary UID must have access to Docker and its own writable
checkout and instance directory; Docker access itself is a host trust boundary.

On an AppArmor host, system mode requires the host's `apparmor_parser` and root
or non-interactive `sudo` permission to load/remove a unique instance profile.
The launcher records its name, version and content hash, adds it in enforcing
mode and verifies the outer PID's actual context. It never replaces
`docker-default` or another host profile. If it cannot manage or enforce its
own profile, startup fails and retains diagnostics. Build mode does not load
profiles or gain permissions. Host modules, sysctls and services are unchanged.
A successful host `check` is only an initial check: `start` exercises the actual
private services, namespaces, delegation and device/syscall interfaces.

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

These suites are a minimal native ARM example. Release qualification also runs
`sandbox.lifecycle.sh`, `snapshot.restore.sh` and `network.tapfd.sh` on ARM; add
those three `--include` options to both commands to exercise that scope. On x86_64 use the
current full ordinary selection, `--all --exclude storage.obs.sh`, for both
prepare and run. Do not remove case architecture guards. Credentialed OBS
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
records remain as evidence; use a new instance name after cleanup. The owned
AppArmor profile is removed only after its container is removed; a foreign
container using the same profile prevents removal.

Each system instance has a distinct machine ID and PID/UTS/IPC/mount/network/
cgroup namespaces, private bpffs, Docker and containerd roots and state, sockets,
and read-only release inputs. It receives KVM/TUN plus SYS_ADMIN, NET_ADMIN and
SYS_PTRACE in addition to Docker's default capabilities. The retained default
seccomp policy permits only the additional userfaultfd, pivot_root and keyctl
operations needed by this environment. It never receives the host Docker
socket, host root, whole host cgroup tree or host BPF pins.

The outer AppArmor profile permits the private mount/cgroup/bpffs and nested
Docker operations while retaining proc/sys/firmware/securityfs restrictions.
Inside this already-confined mount namespace, a read-only null bind masks
`/sys/module/apparmor/parameters/enabled`. This prevents inner dockerd from
trying to manage the host's AppArmor profiles. It does not disable host
AppArmor: inner processes inherit the enforced outer profile, with no profile
transition or securityfs-management permission.

CPU affinity comes from the invoking process's available CPUs, with explicit
CPU, memory and process limits. Free space checks are capacity checks, not disk
quotas. Shared host kernel, disk and NIC contention remains. This environment
is for trusted build/test tasks, not hostile-root multi-tenancy or independent
performance measurements.

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

Before building the image, `python3 -B workbench/test-apparmor-mounts.py --root <new-task-path>`
checks the detection mask, named network namespaces, Docker-style pivot/old-root
propagation, denied unrelated mounts and owned profile cleanup in private namespaces.
It requires host `apparmor_parser`, `aa-exec`, `unshare`, `ip` and root or `sudo -n`;
it does not qualify Docker or KVM. Public x86 CI runs it before the full image test.

On enforcing Ubuntu, `test-apparmor.py --image "$IMAGE" --root <new-task-path>`
checks the outer and inner PID contexts, private mounts/daemon, denied unrelated
mounts, unchanged host enforcement and owned cleanup. Public native x86 CI
requires this check. It intentionally omits KVM/TUN device qualification so it
can run on hosted machines without KVM; it cannot replace full system/KVM E2E.

The bundled [seccomp base](seccomp-default.json) is Moby's default profile at
[65adc7e022c97f55e45c054ff012988027733b87](https://github.com/moby/profiles/blob/65adc7e022c97f55e45c054ff012988027733b87/seccomp/default.json),
with its [Apache 2.0 license](LICENSE.seccomp). `workbench` derives the small
capability/argument-constrained additions per instance; the base remains
unchanged. Upstream distribution licenses remain with the installed tools.
The [outer AppArmor template](apparmor.profile) derives from
[Moby v26.1.3](https://github.com/moby/moby/blob/v26.1.3/profiles/apparmor/template.go),
with its [Apache 2.0 license](LICENSE.apparmor); its nesting changes are maintained here.
