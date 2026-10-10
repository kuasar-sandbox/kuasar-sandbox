[English](README.md) | [简体中文](README_zh.md)

# Workbench

For the first sandbox, follow [Quick Start](../docs/quickstart.md).
[Acquire a release](../docs/download.md) supplies verified inputs and image import
without a source checkout. This guide owns environment/lifecycle semantics. Run
release examples from the extracted directory and source-build examples from the
project checkout, using the matching launcher in each case.

Workbench supplies the native Linux build tools and system environment for a
selected Kuasar Sandbox release. One image supports ordinary UID builds and a
systemd PID 1 environment with private Docker/containerd. It contains external
test image archives, while products, test helpers and Demo wheels come from the
matching release. See the [public E2E guide](../test/README.md) for selection
and the [Demo guide](../test/demo/DEMO.md) for the complete example.

## Host and image

Use native x86_64 or aarch64 Linux with Docker Engine and Python 3.9+. The launcher
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

### Windows and WSL 2 development hosts

WSL is usable only when its Linux environment meets the selected release's
native requirements. A running distribution or `/dev/kvm` alone is insufficient;
Windows, CPU, firmware and any outer hypervisor must expose usable nested
virtualization. This procedure does not qualify every WSL combination or ARM
KVM execution. Workbench cannot supply a missing host capability.

First identify the Windows machine and the exact distribution from local
PowerShell; do not configure an unrelated remote Linux shell:

```powershell
hostname
whoami
Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model,HypervisorPresent,TotalPhysicalMemory
Get-CimInstance Win32_Processor | Select-Object Name,NumberOfLogicalProcessors,VirtualizationFirmwareEnabled,SecondLevelAddressTranslationExtensions
wsl --version
wsl --list --verbose
```

Record the Windows build, available disk space, existing WSL/Docker workloads
and configuration before changes. Correlate firmware/SLAT fields with the
running hypervisor and actual Linux KVM probes: an active hypervisor can hide
hardware fields. If Windows is itself a VM, nested exposure must be enabled by
its outer owner. VM processor configuration applies to that VM, not to an
ordinary physical Windows host.

Use a WSL 2 distribution with a local rootful Linux Docker Engine and its Unix
socket. Check the active Docker context/endpoint, daemon kernel/architecture,
storage driver, data directory and real container networking. Preserve existing
Docker data while choosing the endpoint. Inside the distribution, record
`uname -r`, `uname -m`, `getconf PAGESIZE`, `nproc`, `free -h`, `df -h`, PID 1,
`/sys/fs/cgroup/cgroup.controllers` and the effective cgroup budgets. For a
systemd-managed distro daemon, enable systemd through
[Microsoft's distribution configuration](https://learn.microsoft.com/en-us/windows/wsl/systemd)
and verify PID 1 and `systemctl status docker`; Workbench's inner systemd is a
separate instance. A systemd service does not itself keep WSL alive. Retain logs,
process exit status and a resumable stage record for long builds/tests.

If the installed kernel lacks required features, build the **WSL host kernel**
from a recorded Microsoft tag/commit compatible with the installed WSL release.
Start from that version's Microsoft WSL configuration and retain Hyper-V,
storage, networking and interoperability support. Follow that checkout's
[official build and VHDX instructions](https://github.com/microsoft/WSL2-Linux-Kernel#build-instructions):
the packaging script/layout varies by kernel version. Build matching modules
and the modules/artifacts VHDX, recording the kernel release, config diff and
output hashes. Do not substitute Kuasar's Guest defconfig, alter the Guest ABI,
or change the project's minimum kernel baseline to configure this host.

Audit host features against the selected tests: KVM and the CPU vendor module;
namespaces, cgroup v2 controllers/delegation, seccomp and overlayfs;
userfaultfd, memfd and shmem; BPF/JIT/BTF, bpffs and TC BPF; TUN/TAP, veth,
bridge, GENEVE, conntrack and NAT; and the required vsock/vhost/storage paths.
Check NFS, FUSE and EROFS only for their actual host use; installing a userspace
tool does not enable a kernel feature. Verify module loading against `uname -r`.
On a native Linux host, this privileged KVM preflight opens and closes an empty
VM without persisting state:

```sh
sudo python3 - <<'PY'
import fcntl, os
with open('/dev/kvm', 'rb+', buffering=0) as kvm:
    assert fcntl.ioctl(kvm, 0xAE00, 0) == 12, 'unexpected KVM API version'
    vm = fcntl.ioctl(kvm, 0xAE01, 0)
    os.close(vm)
PY
```

Passing the probe still requires a real Kuasar Guest run. Likewise, verify
userfaultfd operations, BPF loading, delegated cgroups and TAP/NAT communication
through the selected source/product checks; a config symbol or device listing
is not functional acceptance.

Keep the original `%UserProfile%\.wslconfig` and kernel/module artifacts in a
Windows-accessible recovery location. Merge the selected `kernel`,
`kernelModules`, `nestedVirtualization`, `processors`, `memory` and `swap`
settings according to [Microsoft's configuration reference](https://learn.microsoft.com/en-us/windows/wsl/wsl-config),
preserving unrelated settings. These options affect the user's WSL 2 VM;
coordinate an interruption window for all running distributions before
`wsl --shutdown`. After restart, verify the actual kernel/modules and budgets.
If boot fails, restore the backed-up configuration from PowerShell and restart
WSL; if there was no prior configuration, remove only the newly introduced
overrides. Recovery must not require a working Linux shell.

### Resource budgets and acceptance

Budget Windows, the WSL kernel/services and outer Docker separately from the
Workbench limit. Leave CPU and memory headroom at each layer; swap does not
replace RAM required by active VMs. A Workbench example is not a WSL total or a
minimum for the full suite. Keep source/build trees, caches and container data on
the distribution's native Linux filesystem. Account for kernel build outputs,
image archives/layers, prepared inputs, repeated cases and large-image tests.
Check both Linux filesystem free space and free space on the Windows volume
backing its VHDX; the virtual disk's maximum capacity is not available storage.

Inside Workbench, use an explicit node assignment when sharing the environment.
The [node reservation model](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-resource.md#41-pool)
defines `physical_memory: auto` from host `MemTotal`, not the container's memory
limit. Its startup pool is an admission budget, not preallocated RAM:

```text
AllocatablePool = (Physical - HostReserved) * (1 - operational_margin_factor)
StartupPool     = AllocatablePool * startup_factor
```

The implementation uses saturating subtraction and byte rounding. For example,
an assigned 10 GiB, a 1 GiB reserve, a 0.10 margin and a 0.50 startup factor give
about 8.1 GiB allocatable and 4.05 GiB startup admission. Requesting a 6 GiB startup
reservation requires an adequate pool and valid policy; increasing a factor
does not create physical capacity. Retain the node's documented bounds and
measure actual peak memory/OOM events before increasing concurrency. Guest
capacity, current allocation and startup reservation are separate quantities.

Run the matching [Demo](../docs/quickstart.md) to verify actual creation, exec,
file operations, DNS/Internet access, snapshot/resume and cleanup, then the
[ordinary full selection](../test/QUICKSTART.md) on the same native architecture.
Retain versions, case selection, exit codes, logs and peak resource observations.
Missing capabilities fail acceptance; do not reduce workload assertions to
claim a smaller supported machine. OBS needs separate credentials and remains
outside ordinary full selection. For DNS/egress failures, locate the failing
boundary among Windows, WSL, outer container and Guest; inspect that instance's
DNS, proxy, route and MTU before changing it. Do not prescribe a universal DNS
address/MSS, switch firewall backends without evidence or disable host firewalls.

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

The example is a subset, not full coverage. On both native x86_64 and aarch64,
use `--all --exclude storage.obs.sh` for both prepare and run to select the
complete ordinary cases from the chosen release. Full ARM execution requires
actual KVM/kernel prerequisites, a Guest kernel with ARM PMEM/DAX support and
the release's static ARM `cgroup-fork-probe`; missing inputs fail during prepare.
The case count follows the release's files, not a fixed historical count.

For full selection, start with `--network bridge`: `--offline` prohibits
dependency fetches during prepare, while Demo requires real Internet egress
in run. Release qualification also disconnects the owned bridge during prepare
and reconnects it afterward. Keep every architecture, DAX and exit assertion.
Credentialed OBS remains an explicit choice with its own network and credentials.

New native release qualification records `qualification_scope=native-full`
and binds the complete case list, exits, kernel/helper/product provenance and
workbench identity. Historical `system` evidence keeps its original selection;
it cannot be relabelled full. Hosted ARM without KVM retains non-KVM CI and
`artifact-only` release qualification with no claimed system/offline execution.

`--network none` removes external networking from the outer instance; private
Guest, Registry, Store and Proxy traffic remains available. The image defaults
to `E2E_DEPS_DIR=/opt/workbench/deps` and `E2E_OFFLINE=1`. For an intentional
online prepare in an instance started with `--network bridge`, invoke
`exec -- env E2E_OFFLINE=0 python3 ...` without `--offline`. Pass only the task
variables needed by that command. Offline mode controls acquisition, not case
selection. Missing or corrupt required inputs fail; helpers and locked wheels
are always consumed from the selected platform-release, without PATH or global
Python package fallback.

For full Demo or ordinary full-suite runs, create a bridge-network instance;
replacing the subset with `--all` does not give a running none-network instance
egress. Budget CPU/memory/disk for the selected workload; changed budgets or mounts
need a new instance. Two terminals must use the same `--root`/`--name` for `exec`.

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
