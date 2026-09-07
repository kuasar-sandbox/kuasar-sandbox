# kuasar-sandbox — MicroVM sandbox platform overview

Kuasar Sandbox serves Agent, Serverless, Code Interpreter, reinforcement-learning,
and other workloads that need isolated execution, rapid instantiation,
long-lived session state, and elastic resource governance. Each sandbox has an
independent guest kernel. The system supports both creating new instances from a
snapshot template and pausing/resuming the same logical instance.

This document describes public system semantics and component boundaries.
Internal protocols, field layouts, and storage encodings are maintained in the
owning component repositories. Implementation status is determined by the
component's current `main`, the aggregate version selection, and the corresponding
release assets.

## 1. Overview

### 1.1 User scenarios

Kuasar Sandbox addresses these common requirements:

- Agents and Code Interpreters need an independent kernel and filesystem state
  for each session, without repeating all initialization from an empty environment.
- Serverless and parallel jobs need multiple independently identified instances
  of the same prepared environment.
- Reinforcement-learning rollouts need parallel copies of a common initial
  environment, with each trajectory evolving independently.
- Long-lived sessions wait for model calls, human approval, or external events.
  Idle resources should be released, and an instance can be paused when
  appropriate before continuing the same logical session.
- Both single-node and cluster deployments need explicit boundaries for resource
  admission, recovery, network identity, and data portability.

These capabilities support production deployment. Stable and Preview identify
release-channel stability for assets and interfaces; they do not replace the
operator's production configuration for capacity, reliable storage, TLS, network
policy, and credential management.

### 1.2 Design principles

1. **Isolation first**: each sandbox has its own guest kernel. Sharing occurs in
   explicit read-only layers, trusted host data paths, and configured security
   domains.
2. **Stable user semantics**: creating a new instance from a template is distinct
   from resuming the same instance. An upper-layer capacity-preparation strategy
   is not another snapshot format.
3. **Selectable data paths**: local files, shared file storage, and
   Manifest/object-store/cache paths are valid backends. Operators choose based
   on portability, scale, and infrastructure.
4. **Separate control from execution**: the node controller owns admission and
   the shared pool; sandboxer executes per-sandbox resource control. The cluster
   placer chooses placement, not sandbox lifecycle execution.
5. **Separate facts from targets**: label implementation status, test results,
   and design targets separately. Performance numbers require reproducible
   measurement definitions.
6. **Independent components**: the five component repositories can evolve,
   deploy, and release independently. The project repository owns system design,
   cross-component validation, and aggregate releases.

### 1.3 System boundaries

Kuasar Sandbox provides MicroVM lifecycle management, data access, node networking,
and single-node/cluster orchestration. It does not replace:

- external identity systems, tenant billing, business workflows, or global
  capacity planning;
- the object store, shared filesystem, OCI registry, or external policy gateway;
- application-level memory limits, error handling, or data classification;
- capabilities described in issues or RFCs that have not been merged and released.

## 2. User entry points and deployment configuration

### 2.1 User entry points

In a single-node deployment, `node-ctl conductor serve` provides E2B-compatible
control-plane and data-plane entry points. In a multi-node deployment,
`cluster-ctl router` provides a unified entry point and uses the registry and
placer to route group-scoped requests to the target node. Both modes support the
create, execute, pause, connect/resume, and destroy semantics used by an unmodified
E2B SDK.

`sandbox-ctl` is the node-internal execution tool for one sandbox. It starts
MicroVMs, executes commands, takes snapshots, restores, and imports/publishes
artifacts. Ordinary platform users use E2B or platform APIs rather than manipulating
internal sockets or component protocols.

### 2.2 Configuration ownership

Configuration belongs to the component that owns the behavior:

| Scope | Configuration and authoritative documentation |
| --- | --- |
| Per-sandbox startup, disks, snapshots, and resource execution | `sandboxer/docs/sandbox.md` |
| Node API, credentials, builds, and recovery | `orchestrator/docs/node.md` |
| Node admission and the shared resource pool | `orchestrator/docs/node-resource.md` |
| Cluster registry/router/placer | `orchestrator/docs/cluster*.md` |
| Manifest, store, and cache | `accelerator/docs/{manifest,store,cache}.md` |
| vSwitch and network-identity transport | `connector/docs/vswitch.md` |
| Guest runtime and kernel | `guest-runtime/docs/*.md` |

See [deployment.md](deployment.md) for topology, process dependencies, and ports.
The system overview does not duplicate internal fields or wire contracts, or
substitute an old design inference for the current implementation.

## 3. System architecture

### 3.1 Overall topology

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
KVM / Local File / NAS / Object Storage / Network
```

### 3.2 Component responsibilities

| Component | System responsibility |
| --- | --- |
| `orchestrator` | `node-ctl` provides the single-node E2B-compatible service, node admission, and recovery; `cluster-ctl` registry, router, and placer provide the group-scoped cluster control plane |
| `sandboxer` | Drives Cloud Hypervisor and the guest; executes MicroVM creation, running, snapshotting, restore, destruction, and the per-sandbox resource-control loop |
| `accelerator` | Provides local/remote references, Manifest, store, cache, OCI pulling, and EROFS flattening for images, snapshots, and sparse artifacts |
| `connector` | Provides the eBPF/TC vSwitch, TAP handoff, network-resource lifecycle, sandbox isolation, and integration foundations for external gateways |
| `guest-runtime` | Builds and releases two units: the guest runtime image and the guest kernel |

The project repository does not duplicate these implementations. It maintains
system-level design, real-MicroVM cross-component E2E, the common execution
environment, and aggregate releases of exact version combinations.

### 3.3 Dependency boundaries

Component dependencies remain narrow and explicit:

```text
T0: accelerator, connector, guest-runtime/native-deps
T1: sandboxer -> accelerator/pkg/{manifest,image} + connector/pkg/tapfd
T2: orchestrator -> sandboxer/pkg/resource + accelerator
```

Substantial implementations stay inside their component; specific packages form
the cross-repository API. The five components can form a complete platform or be
adopted independently for a particular use case.

## 4. Snapshot and instance semantics

### 4.1 Create new instances from a snapshot template: 1:N

A template completes environment, dependency, and optionally application-process
initialization before publication. Multiple create requests can reference the
same read-only template parent, but each instance receives an independent
Sandbox ID, network identity, credential binding, and incremental state.
Subsequent writes, process progress, and pauses belong only to that instance;
they do not write back to the template or another instance.

This supports Agents, Code Interpreters, RL rollouts, Serverless, and parallel
jobs: the template pays the common initialization cost, and each instance evolves
independently from the same prepared starting point.

### 4.2 Pause and resume the same instance: 1:1

The default pause captures a memory snapshot, preserving process, memory, and
filesystem state for the same logical instance. An explicit Pause `memory:false`
instead captures a Sandbox E artifact without process-memory state; it is
subsequently cold-started rather than resuming the old process. Create's
`autoPauseMemory` selects TTL-expiry capture behavior and does not change the
default of an explicit Pause. See the owning node API specification for the
memory/cold selection rules.

Connect/resume preserves the stable public Sandbox ID. Artifacts retained on the
original node can be restored there. Once published to a named shared-file
location or Manifest, they can be imported and resumed on another node. The
node-internal execution ID may change while the public identity remains stable.

These semantics support waiting for model calls, human approval, external events,
long sessions, and node migration. A `warm pool` is an upper-layer resource
preparation strategy that keeps instances or templates available in advance,
not an independent snapshot technology.

### 4.3 Layered snapshots

Snapshot reuse comes from explicit parent relationships:

```text
template parent (read-only)
        ├── instance A delta
        │       └── later pause delta
        └── instance B delta
                └── later pause delta
```

- Instances created from one template share its parent and maintain their own
  incremental state.
- A later pause can record current disk and memory state as a new layer that
  explicitly references an earlier parent.
- Disk artifacts and memory snapshots record their sources separately; restore
  validates the complete parent chain.
- Local files, named shared files, and Manifest carry the same layering semantics.
  The carrier does not change the user identity model.

Sharing efficiency does not depend on independently running VMs accidentally
producing identical memory bytes. Reuse comes from explicit template parents,
host page-cache sharing of immutable runtime/rootfs data, and content organization
of stable artifacts within the security domain.

## 5. Data paths and accelerator

### 5.1 Unified reference semantics

The lifecycle layer uses unified artifact references and integrity information;
it does not require all data to be converted to one backend first. Valid paths
include:

1. **Local files**: suitable for single-node deployments, local NVMe, and
   node-affine workloads. Images, disk layers, and snapshots can remain native
   files.
2. **Shared file storage**: NAS, NFS, or another shared filesystem provides native
   file access across nodes. A named file location can directly hold snapshots
   and images without first converting them to content chunks.
3. **Manifest / object storage / tiered cache**: suitable for large-scale
   distribution, remote persistence, cross-node restore, on-demand reads, and
   caching hot content. Artifacts do not depend on files at the original node.
   The store backend can be a filesystem or S3-compatible object storage.

Operators can choose different paths for different artifacts. For example,
transient node-local state can use NVMe, snapshots on shared NAS can use named
file locations directly, and widely distributed images can use Manifest with a
tiered cache.

### 5.2 Accelerator's role

`accelerator` is the foundational data-access and storage component for images,
snapshots, and sparse artifacts. It covers:

- ordinary and sparse data representations, with a strict distinction between
  Hole, Zero, and Data;
- local artifacts, sparse-range handling, integrity verification, and optional
  encryption;
- local-file, shared-file, and remote-content references;
- content organization and Manifest;
- local/shared filesystem and S3-compatible store backends;
- tiered caching made up of local cache, distributed cache, and origin access;
- OCI image pulling, deterministic EROFS flattening, and runtime-config projection;
- on-demand reads, working-set loading, and explicit prefetch.

Content addressing and deduplication are capabilities within this scope, not the
component's only value. Instances of one template primarily share the parent.
Content-level reuse is particularly useful for stable images, runtimes, and
repetitive artifacts, within the content-key and security-domain boundaries.

### 5.3 Sparse and on-demand data

Sparse semantics have three states: Hole comes from authoritative metadata, Zero
means logical zero data, and Data means actual content. The system does not scan
zero-valued bytes to invent Holes. Snapshots and disk layers can be read by
referenced ranges. Cache hits and prefetch reduce remote accesses without changing
artifact integrity or parent relationships.

## 6. High-density resource governance

Agent sandboxes often wait for long periods and execute short bursts. The system
improves effective node utilization through this loop:

```text
long wait, short burst
        │
        ▼
release idle CPU and inactive memory
        │
        ▼
pause long waits to snapshots and return resources to the node pool
        │
        ▼
admission + dynamic budget + watermarks + reclaim + safety margin
        │
        ▼
protect active work and safely host more logical sandboxes
```

Responsibilities are separated as follows:

| Component | Responsibility |
| --- | --- |
| `sandboxer` | Applies per-sandbox resource configuration and coordinates the guest balloon, host cgroup, and VMM lifecycle for growth, shrink, pause, and resume |
| `node-ctl` Reservation Controller | Manages node admission, the shared resource pool, watermarks, Grant, Inventory, recovery, and node safety margin |

The node controller neither reads/modifies sandbox cgroups nor directly drives
the VMM balloon. It manages Reservation and Grant through the resource protocol.
Growth first obtains a node Grant, then increases the sandbox budget. Shrink first
converges in the per-sandbox loop, then releases Reservation. Long-inactive
instances can be paused to return execution resources to the node pool.

The protection boundary is the sandbox's declared resources and the node's
admission model: node-level resource reuse should not cause sandbox OOM or loss
of useful work within that boundary. Applications can still encounter OOM because
of their own declarations, memory limits, or behavior; there is no unconditional
OOM guarantee.

High density results from improved utilization, not an isolated pursuit of an
instance count. Capacity must be validated against real workload peak working
sets, active fractions, recovery cost, and node safety margin.

## 7. Networking and multitenancy boundaries

### 7.1 Networking objectives

`connector` provides these foundations for sandbox networking:

- rapid allocation, recovery, and release of network resources;
- high-speed eBPF/TC forwarding in the kernel;
- default isolation between sandboxes, without arbitrary port-to-port forwarding;
- trusted sandbox network identity generated and checked by the platform rather
  than accepted from a guest's self-reported identity;
- transport of that trusted identity to an external policy gateway;
- integration foundations for sandbox-level public/private networking, DNS,
  proxying, and audit policy.

The system overview defines these objectives. Network-location encoding,
encapsulation fields, and option layout belong to the connector specification,
not the primary user-facing interface.

### 7.2 Feature status

| Capability | Status | Meaning |
| --- | --- | --- |
| Basic vSwitch, port lifecycle, and sandbox isolation | Delivered | Maintained on connector main and covered by component E2E |
| Management-service path and external-gateway integration foundations | Delivered | vSwitch supplies trusted ingress/egress and identity-transport foundations |
| Central policy gateway | Integration supported | The operator's external gateway enforces public/private networking, DNS, proxy, and audit policy |
| Lightweight node-local Egress | Proposed | [`connector#9`](https://github.com/kuasar-sandbox/connector/issues/9) is a design proposal, not a currently delivered vSwitch capability |
| OpenTelemetry | Proposed | Tracked by [`kuasar-sandbox#52`](https://github.com/kuasar-sandbox/kuasar-sandbox/issues/52) and implementation work in progress, not a current deployment prerequisite |

Delivered means merged into component main with component validation; it does not
necessarily mean included in an aggregate Stable release. The existence of an
issue, RFC, or PR does not make a Proposed capability delivered.

### 7.3 Multitenant security

- Platform identity credentials and content-protection keys have separate uses.
  APISecret authenticates platform identity/control-plane access; ManifestKey
  protects the content-key domain.
- The control plane, ordinary data plane, and remote execution use credentials
  with different scopes. Remote-execution tokens bind a particular subject,
  purpose, and conditions.
- Content-protection keys stay in the trusted host data path and are not injected
  into the guest.
- Local and remote artifacts can be encrypted and verified according to deployment
  policy. Key protection, backup, and rotation remain operator responsibilities.
- Content keys and security-domain configuration determine deduplication and
  sharing boundaries. Identical content does not itself justify cross-tenant sharing.
- Stable Sandbox ID, credential binding, and trusted network-identity boundaries
  remain in force across pause, resume, and migration.
- Network isolation, node resource admission, fault domains, and encryption jointly
  provide multitenant security; no single mechanism replaces the others.

Credential updates affect records subsequently created with copied credentials.
Credentials already persisted in existing sandbox records retain their original
binding; changing the key-distribution table does not automatically rebind them.

## 8. Clustering and reliability

### 8.1 Single-node and cluster operation

`node-ctl conductor serve` manages local sandboxes, builds, data-plane proxying,
and the optional Reservation Controller. A cluster has three independent
`cluster-ctl` roles:

- `registry` maintains reliable execution state for nodes, routes, and placers;
- `router` provides the unified E2B-compatible entry point and sandbox-group routing;
- `placer` imports group configuration and chooses candidate nodes; it does not
  own sandbox lifecycle execution.

The target node and sandboxer still execute the lifecycle. Public Sandbox ID
remains stable across same-node resume, cross-node migration, and replacement;
the node-internal execution ID can change with the placement generation.

### 8.2 Recovery and fault domains

- `sandbox-ctl`, VMM, node, registry, router, and placer are independently observable
  and recoverable processes/fault domains.
- Persistent node records and runtime Inventory support restart recovery and orphan
  cleanup. A host socket's existence alone does not prove guest readiness; verify
  recovery with a real health or execution operation.
- Local artifacts support node-affine recovery. Shared-file and Manifest artifacts
  can be imported and restored independently of the original node.
- The registry stores cluster execution state; the node is authoritative for running
  sandboxes. The placer proposes placement, and node admission confirms resources.
- Group is the cluster partition key. Routing, placement, credential validation,
  and operations remain group-scoped.

### 8.3 Versions and validation

Components have independent versions and releases. The project aggregate selects
six exact release units: four component archives, the guest runtime, and the
guest kernel. Aggregate preparation validates component assets and SHA256 values,
then runs owner E2E and platform integration cases in a real KVM environment.
The release records the precise combination rather than mixing assets from
different dates or aggregate versions.

These mechanisms support version pinning, fault diagnosis, and reproducible
production validation. Operators still need production-grade TLS, persistence,
backups, monitoring, network policy, and capacity configuration.

## 9. Performance and capacity

The system overview does not present one-off measurements or capacity calculations
as universal capabilities. [perf.md](perf.md) defines performance reporting and
regression through component-owned E2E/performance entry points.

An externally comparable result must include at least:

- the aggregate version and all relevant component versions or exact commits;
- CPU, memory, disk, network, KVM, and host software environment;
- local-file, shared-file, or object-store data path;
- cache levels and cold/hot state;
- sandbox vCPU, memory, disks, and workload;
- sample count, aggregation method, percentiles, and failure rate;
- the events defining the measured latency or throughput;
- the raw report or reproducible test entry point.

Without this context, a number cannot promise node capacity, startup/restore
latency, cache hit rate, or storage savings. Label design goals as targets.
Only validation at a specified version and environment produces a result within
that test's scope.

## 10. Deployment and release status

Kuasar Sandbox provides single-node and cluster topologies, node resource
admission/recovery, independent processes and fault domains, independently
versioned components and aggregate releases, real-MicroVM cross-component E2E,
release-asset validation, and exact version combinations.

The Stable aggregate release at this document's review is
[`release-v0.1.2`](https://github.com/kuasar-sandbox/kuasar-sandbox/releases/tag/release-v0.1.2).
Preview remains available for development and evaluation. GitHub releases currently
provide Linux x86_64 prebuilt assets. Source builds support x86_64 and aarch64;
a buildable source architecture is not automatically a published asset architecture.
Consult [GitHub Releases](https://github.com/kuasar-sandbox/kuasar-sandbox/releases)
and [release.md](release.md) for the latest state.

## 11. See also

- [deployment.md](deployment.md) — topology, processes, ports, and start/stop dependencies.
- [perf.md](perf.md) — measurement context and performance regression methodology.
- [release.md](release.md) — component/aggregate versions, assets, and publishing transactions.
- [Demo](../test/demo/DEMO.md) — local evaluation environment and E2B SDK demonstration.
- [Full validation](../test/QUICKSTART.md) — complete aggregate-release validation.
- [`orchestrator`](https://github.com/kuasar-sandbox/orchestrator/tree/main/docs) — node, resources, and cluster design.
- [`sandboxer`](https://github.com/kuasar-sandbox/sandboxer/tree/main/docs) — MicroVM, snapshots, and guest coordination.
- [`accelerator`](https://github.com/kuasar-sandbox/accelerator/tree/main/docs) — Manifest, store, and cache.
- [`connector`](https://github.com/kuasar-sandbox/connector/blob/main/docs/vswitch.md) — vSwitch and network details.
- [`guest-runtime`](https://github.com/kuasar-sandbox/guest-runtime/tree/main/docs) — runtime, vmlinux, and flattening.
