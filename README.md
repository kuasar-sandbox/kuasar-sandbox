[English](README.md) | [简体中文](README_zh.md)

# Kuasar Sandbox

**Kuasar Sandbox is a production-ready MicroVM sandbox platform for AI agents, serverless workloads, and reinforcement-learning environments.**

It provides independent guest-kernel isolation, snapshot-template instantiation, stateful pause and resume, on-demand data loading, high-density resource governance, and a deployment path from a single node to a multi-node cluster.

The current stable aggregate release is [`release-v0.1.2`](https://github.com/kuasar-sandbox/kuasar-sandbox/releases/tag/release-v0.1.2). Preview releases remain available for development and evaluation. Production readiness describes the system's deployment and operational capabilities; the Stable/Preview label describes the stability of a public release channel. Production deployments should still validate capacity against their own workloads and configure production TLS, durable storage, network policy, and credentials.

## Start here

- [Quick Start](docs/quickstart.md) — download one aggregate release, verify it, and run a real MicroVM through the unmodified E2B SDK;
- [Architecture](docs/kuasar-sandbox.md) — system capabilities, component boundaries, and lifecycle semantics;
- [Deployment](docs/deployment.md) — standalone and cluster topologies, processes, and dependencies;
- [Releases](docs/release.md) — component versions, aggregate versions, asset contracts, and release transactions;
- [Demo](test/demo/DEMO.md) — the complete local demonstration environment;
- [Full validation](test/QUICKSTART.md) — aggregate-release validation and all owner E2E suites;
- [Security](SECURITY.md) — supported versions and private vulnerability reporting;
- [Contributing](CONTRIBUTING.md) — project-repository contribution rules and the organization-wide guide.

## Why MicroVM sandboxes

Agent workloads may execute model-generated commands, user-provided programs, third-party repositories, downloaded tools, and temporary dependencies. The platform therefore cannot assume that code running inside a sandbox is trusted.

Kuasar Sandbox uses KVM MicroVMs as the compute boundary. Each sandbox has its own guest kernel, while the host exposes only controlled lifecycle, storage, networking, and execution interfaces. The platform keeps the security boundary of a virtual machine while specializing the surrounding system for short-lived, highly concurrent, and stateful agent workloads.

## Core capabilities

- **Independent kernel isolation** — each sandbox runs with its own guest kernel behind a KVM MicroVM boundary.
- **Snapshot-template instantiation** — build and initialize an environment once, then create multiple independent instances from the same read-only parent state.
- **Stateful pause and resume** — preserve the memory, processes, and filesystem changes of one logical sandbox and resume it later, potentially on another node when its artifacts are portable.
- **On-demand loading** — memory pages and disk blocks are loaded when the workload actually accesses them, so startup and restore costs follow the working set rather than the full logical image size.
- **Flexible data paths** — images, snapshots, and sparse artifacts can use local files, shared filesystems such as NAS/NFS, or Manifest-backed object storage with tiered caches.
- **High-density resource governance** — idle CPU and inactive memory can be returned to the node pool, while admission, dynamic budgets, watermarks, active reclaim, and safety reserves protect concurrently active work.
- **Sandbox-level networking** — the network layer allocates and releases connectivity quickly, forwards in the kernel, isolates sandboxes by default, and supplies trusted sandbox identity to external policy gateways.
- **Multi-tenant security** — platform identity, scoped data-plane capabilities, content-protection keys, network identity, and node resource budgets remain separate security boundaries.
- **E2B-compatible entry point** — an unmodified E2B SDK can create, execute, pause, reconnect to, and terminate sandboxes.
- **Standalone and cluster deployment** — the same components can run on one node or be assembled into a Registry/Router/Placer multi-node control plane.

## Snapshot lifecycle

Kuasar Sandbox exposes one snapshot infrastructure through two user-facing workflows:

### Create instances from a snapshot template (1:N)

A template can contain an initialized operating system, language runtime, dependencies, tools, and warmed services. Multiple new sandboxes share its immutable parent state while keeping independent identity and writable changes.

### Pause and resume one logical instance (1:1)

A running sandbox can be paused with its process, memory, and filesystem state preserved. Reconnecting to the same stable sandbox identity resumes that logical instance. This supports long-running agents, human approval steps, idle sessions, node maintenance, and migration.

These workflows use explicit parent/child snapshot relationships. Storage efficiency comes first from sharing the known parent layer and recording only instance-specific changes; it does not depend on independently running virtual machines accidentally producing highly identical memory snapshots.

## Data and storage model

Kuasar Sandbox does not require every artifact to use one storage backend:

| Data path | Typical use | Characteristics |
| --- | --- | --- |
| Local files | Standalone nodes, local NVMe, node-affine workloads | Short path and few external dependencies |
| Shared filesystems | NAS, NFS, or another filesystem mounted consistently across nodes | Native file semantics and direct cross-node access |
| Manifest and object storage | Large-scale distribution, remote persistence, cross-node restore, and tiered caching | Content-addressed organization and on-demand reads |

`accelerator` provides the common data capabilities behind these choices: sparse-data representation, local artifacts, integrity and encryption, references, Manifest organization, filesystem and S3-compatible stores, caches, OCI retrieval, EROFS image flattening, fetch, and prefetch. Content addressing and deduplication are available where they fit the data and security domain; they are not the only storage model and are not assumed to be effective for every independently captured VM snapshot.

## High-density resource governance

Agent sandbox load is highly non-linear: sandboxes often spend most of their lifetime waiting for a model, a tool, external I/O, or human input, then become active in short bursts.

Kuasar Sandbox treats density as an outcome of safe resource utilization:

1. idle sandboxes release CPU and inactive memory;
2. long-idle sandboxes can be paused into durable state;
3. reclaimed capacity returns to the node-wide pool;
4. node admission, dynamic grants, watermarks, active reclaim, and safety reserves protect simultaneous bursts;
5. platform-level sharing must not turn node pressure into sandbox OOMs or lost valid work within the declared resource model.

The `sandboxer` runtime executes per-sandbox cgroup, balloon, and VMM lifecycle actions. The `node-ctl` reservation controller owns node-wide admission, resource-pool accounting, grants, inventory reconciliation, and recovery. High density therefore means maximizing useful node utilization without using OOM as a scheduling mechanism, not merely maximizing a raw VM count.

## Networking and policy integration

`connector` provides a high-density eBPF/TC data path for MicroVM networking. It creates and releases sandbox network resources quickly, keeps sandbox-to-sandbox forwarding absent by default, validates platform-assigned network identity, and forwards established traffic without requiring a user-space process on every packet.

Deployments can carry trusted sandbox identity to a centralized external policy gateway, where sandbox-level public-network, private-network, DNS, proxy, audit, and traffic-governance policy can be enforced. A node-local lightweight Egress plane is tracked as a proposed extension; it is not presented as an already delivered part of the base vSwitch.

## Architecture

```text
Agent / E2B SDK / Platform API
                │
           orchestrator
                │
        sandboxer runtime
        ├── accelerator
        ├── connector
        └── guest-runtime
                │
KVM / Local Files / NAS / Object Storage / Network
```

`kuasar-sandbox/kuasar-sandbox` is the canonical project repository. It owns the system-level design, shared BMS infrastructure, cross-component validation, demos, aggregate version selection, and aggregate releases.

The implementation is maintained in five loosely coupled component repositories:

| Component repository | Responsibility |
| --- | --- |
| [`orchestrator`](https://github.com/kuasar-sandbox/orchestrator) | E2B-compatible node service, node resource admission, proxy integration, and Registry/Router/Placer cluster control plane |
| [`sandboxer`](https://github.com/kuasar-sandbox/sandboxer) | MicroVM lifecycle, snapshot and restore, guest control, block devices, and per-sandbox resource execution |
| [`accelerator`](https://github.com/kuasar-sandbox/accelerator) | Data access, storage, encryption, content organization, caching, OCI retrieval, and image flattening |
| [`connector`](https://github.com/kuasar-sandbox/connector) | High-density eBPF networking, isolation, TAP handoff, trusted sandbox network identity, and policy-gateway integration foundations |
| [`guest-runtime`](https://github.com/kuasar-sandbox/guest-runtime) | Guest runtime image, guest kernel, native guest dependencies, and image-building tools |

The five components form the complete platform together, but each component can evolve, build, deploy, and release independently. `guest-runtime` produces two release units, Runtime and VMLinux, while remaining one component repository.

## Release status and support scope

- **Current stable aggregate release:** [`release-v0.1.2`](https://github.com/kuasar-sandbox/kuasar-sandbox/releases/tag/release-v0.1.2).
- **Preview channel:** GitHub prereleases for development and evaluation; Preview does not replace the current Stable release.
- **Prebuilt architecture:** current GitHub releases provide Linux x86_64 assets.
- **Source-build architectures:** the Makefiles support `TARGET_ARCH=x86_64` and `TARGET_ARCH=aarch64`; source-build support does not mean that prebuilt release assets are published for both architectures.
- **Version relationship:** components release independently; an aggregate release selects exact component versions and validates the combined system on real KVM infrastructure.

`Stable` means a non-prerelease aggregate release. `Preview` means a prerelease aggregate release for development and evaluation. `Proposed` means a design or issue that must not be treated as delivered functionality.

| Scope | Status |
| --- | --- |
| Standalone node | Available through `node-ctl conductor serve` and the E2B-compatible entry point |
| Multi-node cluster | Available; requires Registry, Router, Placer, reachable node services, and cluster infrastructure |
| Local, shared-file, and object-storage data paths | Available; select according to locality, shared access, and persistence requirements |
| Base vSwitch and sandbox isolation | Available |
| Centralized external policy-gateway integration | Supported integration foundation |
| Node-local lightweight Egress plane | [Proposed](https://github.com/kuasar-sandbox/connector/issues/9) |
| OpenTelemetry integration | [Proposed](https://github.com/kuasar-sandbox/kuasar-sandbox/issues/52) |

See [GitHub Releases](https://github.com/kuasar-sandbox/kuasar-sandbox/releases) for the authoritative asset list and prerelease state.

## Quick Start

The [Quick Start](docs/quickstart.md) downloads every explicit asset from one aggregate release, verifies `SHA256SUMS`, prepares a standalone node, and uses the upstream E2B Python SDK to:

```python
from e2b import Sandbox, Template

template = Template().from_image("<registry>/<image>:<tag>")
build = Template.build(template, name="quickstart", cpu_count=2, memory_mb=6144)
sandbox = Sandbox.create(build.template_id, timeout=300)
sandbox_id = sandbox.sandbox_id
print(sandbox.commands.run("uname -sm").stdout)
sandbox.pause()
sandbox = Sandbox.connect(sandbox_id)  # reconnect and auto-resume
print(sandbox.commands.run("echo resumed").stdout)
sandbox.kill()
```

For the complete local environment, template fan-out, network access, and migration flow, see the [Demo](test/demo/DEMO.md).

## Source workspace

The standard development workspace places the project repository and five component repositories next to one another:

```text
<workspace>/
├── kuasar-sandbox/
├── accelerator/
├── connector/
├── sandboxer/
├── guest-runtime/
└── orchestrator/
```

The project repository contains:

```text
docs/              System design, deployment, performance, CI, and release documents
test/e2e/          Owner suites, run_all.sh, and platform integration cases
test/perf|demo/    Platform performance and demonstration scripts
ci/bms/            BMS helpers and source-cache maintenance
ci/native-cache/   VMLinux, EROFS, Envd, RocksDB, and Cloud Hypervisor cache support
ci/runner/         Self-hosted BMS runner deployment
release/           Version resolution, packaging, aggregation, and publishing
releases/          Stable and daily Preview aggregate selections
.github/workflows/ Reusable BMS, aggregate-release, and daily-Preview workflows
```

## Build and test

A complete source build is driven from this project repository and consumes the five sibling component repositories:

```bash
make -C kuasar-sandbox build
make -C kuasar-sandbox test
make -C kuasar-sandbox test-e2e
make -C kuasar-sandbox perf
make -C kuasar-sandbox demo
```

Runtime files are collected into `bin/<arch>/` according to [`release/bin-inputs.manifest`](release/bin-inputs.manifest). Each component also supports its documented standalone build. For example:

```bash
GOWORK=off make -C sandboxer build
```

Release-tool tests do not contact GitHub or build the real native dependencies:

```bash
make -C kuasar-sandbox test-release-tools
make -C kuasar-sandbox test-ci-tools
make -C kuasar-sandbox test-perf-tools
```

Tests requiring KVM, root, eBPF, systemd, external storage, or the complete source workspace document those prerequisites separately. A skipped privileged suite must not be interpreted as a completed integration validation.

## Release model

The component repositories publish independent version lines:

- `accelerator`, `connector`, `sandboxer`, and `orchestrator`: `vX.Y.Z`;
- guest runtime: `runtime-vX.Y.Z`;
- guest kernel: `vmlinux-vX.Y.Z`.

The project repository publishes aggregate versions named `release-vX.Y.Z`. An aggregate version may select different version numbers for different components. `releases/release.yaml` selects the next Stable aggregate, while `releases/daily-preview.yaml` selects the current daily Preview. The aggregate workflow resolves exact component tags, verifies the declared assets and checksums, assembles the platform archive, and runs cross-component validation before publishing.

Current x86_64 aggregate releases contain one platform archive, six release-unit archives, and one aggregate `SHA256SUMS`. The source repositories remain independent; the aggregate release is the tested composition contract.

See [docs/release.md](docs/release.md) for the full asset contract, trust boundaries, failure recovery, and Preview state machine.

## Documentation

- [Architecture](docs/kuasar-sandbox.md) — system goals, architecture, lifecycle, storage, resource, network, and security semantics;
- [Deployment](docs/deployment.md) — standalone and cluster topology, processes, ports, persistent paths, and startup dependencies;
- [Performance](docs/perf.md) — performance baselines with test context, regression gates, and tuning methods;
- [CI](docs/ci.md) — reusable BMS, source selection, native caches, and exact-asset validation;
- [Releases](docs/release.md) — component versions, aggregate selection, assets, and release transactions;
- [Quick Start](docs/quickstart.md) — the shortest path from an aggregate release to a real MicroVM;
- [Demo](test/demo/DEMO.md) — the complete local demonstration and SDK flow;
- [Full validation](test/QUICKSTART.md) — aggregate package validation and all E2E owners;
- [Security](SECURITY.md) — supported versions and private vulnerability reporting.

Detailed design documents are currently maintained primarily in Chinese. The English README and Quick Start provide the complete public entry path; additional translations can be added without making a full-document translation a prerequisite for source publication.

## Contributing and security

Read the organization-wide [contribution guide](https://github.com/kuasar-sandbox/.github/blob/main/CONTRIBUTING.md) before opening a change. Choose the repository that owns the behavior, keep pull requests focused, and link companion pull requests in both directions when a change spans repositories.

Do not report security vulnerabilities in public issues or discussions. Follow the [Security Policy](SECURITY.md) and use GitHub private vulnerability reporting.

## License

Original project content in this repository is licensed under the [Apache License 2.0](LICENSE). Third-party and differently licensed content retains its own declarations and obligations. See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution licensing.
