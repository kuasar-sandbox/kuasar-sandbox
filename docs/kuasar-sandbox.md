[English](kuasar-sandbox.md) | [简体中文](kuasar-sandbox_zh.md)

<a id="kuasar-sandbox---microvm-沙箱平台系统总览"></a>
# kuasar-sandbox - MicroVM sandbox platform overview

Kuasar Sandbox serves Agent, Serverless, Code Interpreter and reinforcement-learning workloads that need isolated execution, fast instantiation, long-lived session state and elastic resource management. Each sandbox has an independent guest kernel as its isolation boundary. The system can create new instances from snapshot templates or pause and resume the same logical instance.

This document describes public system semantics and component boundaries. Internal protocols, field layouts and storage encodings belong to the component repositories. Implementation status is determined by the components' current `main`, the aggregate version selection and the corresponding release assets.

<a id="1-概述"></a>
## 1. Overview

<a id="11-用户场景"></a>
### 1.1 User scenarios

Kuasar Sandbox addresses these shared needs:

- Agents and Code Interpreters need independent kernel and filesystem state for each session, while avoiding complete initialization from an empty environment every time.
- Serverless and parallel tasks need multiple instances with independent identities created from one prepared environment.
- Reinforcement-learning rollouts need parallel copies of a common initial environment, with each trajectory evolving independently.
- Long sessions wait for model calls, human confirmation or external events. Idle resources should be released during those waits; an instance can be paused when appropriate and later continue the same logical session.
- Both standalone and cluster deployments need explicit boundaries for admission, recovery, network identity and data portability.

The system's capabilities support production deployment. Stable and Preview describe the release stability of public assets and interfaces; they do not replace production configuration of capacity, durable storage, TLS, network policy and credentials by the operator.

<a id="12-设计原则"></a>
### 1.2 Design principles

1. **Isolation first:** each sandbox has its own guest kernel. Sharing takes place through explicit read-only layers, trusted host data paths and configured security domains.
2. **Stable user semantics:** distinguish creating a new instance from a template from resuming the same instance. An upper-level capacity-preparation policy is not another snapshot format.
3. **Selectable data paths:** local files, shared file storage and Manifest/object-storage/cache paths are all valid backends. Operators choose based on portability, scale and infrastructure.
4. **Separate control and execution:** the node controller owns admission and the shared pool; sandboxer executes resource operations for an individual sandbox. The cluster placer owns placement, not sandbox lifecycle.
5. **Separate facts from goals:** identify implementation status, test results and design targets separately. Performance numbers require reproducible measurement definitions.
6. **Independent components:** the five component repositories can evolve, deploy and release independently. The project repository owns system design, cross-component validation and aggregate releases.

<a id="13-系统边界"></a>
### 1.3 System boundaries

Kuasar Sandbox provides MicroVM lifecycle, data access, node networking and standalone/cluster orchestration. It does not replace:

- external identity systems, tenant billing, business workflows or global capacity planning;
- object storage, shared filesystems, OCI registries or external policy gateways themselves;
- application-level memory limits, error handling or data classification;
- capabilities described in issues or RFCs that have not yet been merged and released.

<a id="2-用户入口与部署配置"></a>
## 2. User entry points and deployment configuration

<a id="21-用户入口"></a>
### 2.1 User entry points

In a standalone deployment, `node-ctl conductor serve` provides E2B-compatible control-plane and data-plane entry points. In a multi-node deployment, `cluster-ctl router` provides a unified entry point and uses registry and placer to route group-scoped requests to the target node. Both modes support the create, execute, pause, connect/resume and destroy semantics used by the unmodified E2B SDK.

`sandbox-ctl` is the node-internal execution tool for an individual sandbox. It starts MicroVMs and handles execution, snapshotting, restoration and artifact import/publication. Ordinary platform users access the system through E2B or platform APIs and do not need to manipulate internal sockets or component protocols.

<a id="22-配置归属"></a>
### 2.2 Configuration ownership

The component that owns a behavior also owns its configuration:

| Scope | Configuration and authoritative documentation |
| --- | --- |
| Individual sandbox startup, disks, snapshots and resource execution | `sandboxer/docs/sandbox.md` |
| Node API, credentials, builds and recovery | `orchestrator/docs/node.md` |
| Node admission and shared resource pool | `orchestrator/docs/node-resource.md` |
| Cluster registry/router/placer | `orchestrator/docs/cluster*.md` |
| Manifest, store and cache | `accelerator/docs/{manifest,store,cache}.md` |
| vSwitch and network identity handoff | `connector/docs/vswitch.md` |
| Guest runtime and kernel | `guest-runtime/docs/*.md` |

See [deployment.md](deployment.md) for topology, process dependencies and ports. This overview does not duplicate internal fields or wire contracts: an inference from an old design must not replace the current implementation.

<a id="3-系统架构"></a>
## 3. System architecture

<a id="31-总体拓扑"></a>
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

<a id="32-组件职责"></a>
### 3.2 Component responsibilities

| Component | System responsibility |
| --- | --- |
| `orchestrator` | `node-ctl` provides standalone E2B-compatible service, node admission and recovery; the `cluster-ctl` registry, router and placer provide the group-scoped cluster control plane |
| `sandboxer` | Drives Cloud Hypervisor and the guest, implementing MicroVM creation, execution, snapshots, restoration, destruction and the individual sandbox resource loop |
| `accelerator` | Provides local/remote references, Manifest, store, cache, OCI retrieval and EROFS flattening for images, snapshots and sparse artifacts |
| `connector` | Provides the eBPF/TC vSwitch, TAP handoff, network-resource lifecycle, sandbox isolation and foundations for external-gateway integration |
| `guest-runtime` | Builds and publishes two release units: the guest runtime image and guest kernel |

The project repository does not duplicate these implementations. It maintains system-level design, real MicroVM cross-component E2E tests, the shared execution environment and aggregate releases selecting exact version combinations.

<a id="33-依赖边界"></a>
### 3.3 Dependency boundaries

Component dependencies remain explicit. At the repository level:

```text
T0: accelerator, connector, guest-runtime/native-deps
T1: sandboxer -> accelerator + connector/pkg/tapfd
T2: orchestrator -> sandboxer + accelerator + connector
```

Implementation stays inside its owning component, with specific packages exported across repositories. The diagram describes repository-level relationships, not an exhaustive package allowlist: orchestrator uses sandboxer resource and artifact/restore APIs, for example, and its MMDS source index uses connector vSwitch constants. The current module files and imports define the actual dependency set. The five components can form the complete platform or be adopted independently for particular scenarios.

<a id="4-快照与实例语义"></a>
## 4. Snapshot and instance semantics

<a id="41-从快照模板创建新实例1n"></a>
### 4.1 Creating new instances from a snapshot template: 1:N

A template completes environment, dependency and optional application-process initialization before publication. Multiple creation requests can reference the same read-only template parent, but each instance receives its own Sandbox ID, network identity, credential binding and incremental state. Subsequent writes, process progress and pauses belong only to that instance and do not modify the template or other instances.

This serves Agent, Code Interpreter, RL rollout, Serverless and parallel-task workloads: the template bears the common initialization cost, and each instance evolves independently from the same starting point.

<a id="42-暂停并恢复同一个实例11"></a>
### 4.2 Pausing and resuming the same instance: 1:1

A pause that captures memory preserves the process, memory and filesystem state of the same logical instance. Explicit pause defaults to this snapshot behavior. With `memory: false`, pause preserves disk artifacts and later starts the application again; it does not restore the previous process memory. `autoPauseMemory` independently selects the capture behavior of automatic pause. Connect/resume retains the stable public Sandbox ID in either case. Artifacts kept on the original node allow in-place recovery; after publication to a named shared-file location or Manifest, the cluster can import and resume them on another node. The node-internal execution ID can change while the public identity remains stable.

This serves waits for model calls, human confirmation or external events, long sessions and node migration. A `warm pool` is an upper-level resource-preparation policy that can keep instances or templates available in advance; it is not an independent snapshot technology.

<a id="43-分层快照"></a>
### 4.3 Layered snapshots

Snapshot reuse comes from explicit parent relationships:

```text
template parent (read-only)
        ├── instance A delta
        │       └── later pause delta
        └── instance B delta
                └── later pause delta
```

- Instances created from the same template share the template parent and maintain their own deltas.
- A later pause can capture current disk and memory state as a new layer with an explicit reference to the previous parent.
- Disk artifacts and memory snapshots record their origins separately; restoration validates the complete parent chains.
- Local files, named shared files and Manifest carry the same layering semantics. The carrier does not change the user identity model.

Sharing efficiency does not depend on different running VMs coincidentally producing identical memory bytes. Reusable content comes from explicit template parents, the host page cache for immutable runtime/rootfs content and content organization of stable artifacts within a security domain.

<a id="5-数据路径与-accelerator"></a>
## 5. Data paths and accelerator

<a id="51-统一引用语义"></a>
### 5.1 Unified reference semantics

The upper-level lifecycle uses common artifact references and integrity information rather than requiring all data to be converted to one backend. The supported paths include:

1. **Local files:** suitable for standalone nodes, local NVMe and node-affine workloads. Images, disk layers and snapshots can retain their native file representation.
2. **Shared file storage:** NAS, NFS or another shared filesystem provides native file access across nodes. A named file location can hold snapshots and images directly, without first converting them to content chunks.
3. **Manifest / object storage / tiered cache:** suitable for large-scale distribution, remote persistence, cross-node recovery, on-demand reads and hot-content caching. Artifacts do not depend on files on the original node; the store backend can be a filesystem or S3-compatible object storage.

Operators can select different paths for different artifacts. For example, temporary node-local state can use NVMe, shared NAS snapshots can use file locations directly, and widely distributed images can use Manifest and tiered caching.

<a id="52-accelerator-定位"></a>
### 5.2 accelerator's role

`accelerator` is the foundational data-access and storage component for images, snapshots and sparse artifacts. It covers:

- ordinary and sparse data representations, strictly distinguishing Hole, Zero and Data;
- local artifacts, sparse-region handling, integrity verification and optional encryption;
- local-file, shared-file and remote-content references;
- content organization and Manifest;
- local/shared filesystems and S3-compatible store backends;
- tiered caching combining local cache, distributed cache and origin access;
- OCI image retrieval, deterministic EROFS flattening and runtime-config projection;
- on-demand reads, working-set loading and explicit prefetch.

Content addressing and deduplication are capabilities within this scope, not accelerator's sole value. Instances from a common template primarily rely on parent-layer sharing. Content-level reuse is more suitable for stable images, runtimes and repetitive artifacts, and is constrained by content-key and security-domain boundaries.

<a id="53-稀疏与按需数据"></a>
### 5.3 Sparse and on-demand data

Sparse semantics have three states: Hole comes from authoritative metadata, Zero denotes logical zero data and Data denotes actual content. The system does not scan zero bytes to invent Hole regions. Snapshots and disk layers can be read by reference range. Cache hits and prefetch reduce remote access without changing artifact integrity or parent relationships.

<a id="6-高密资源治理"></a>
## 6. Resource management for high density

Agent sandboxes typically alternate long waits with short bursts. The system improves effective node-resource utilization through this loop:

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
| `sandboxer` | Applies individual sandbox resource configuration and coordinates guest Balloon, host Cgroup and VMM lifecycle for growth, shrink, pause and resume |
| `node-ctl` Reservation Controller | Manages node admission, the shared resource pool, watermarks, Grant, Inventory, recovery and node safety margin |

The node controller does not inspect or modify sandbox Cgroups or directly call VMM Balloon operations. It manages Reservation and Grant through the resource protocol. Growth first obtains a node Grant and then expands the sandbox budget. Shrink first converges through the individual sandbox loop, then releases the Reservation. Long-inactive instances can be paused to return execution resources to the node pool.

The platform's protection boundary is to avoid OOM and loss of useful work caused by node-level resource reuse within the sandbox's declared resources and the node admission model. Applications can still encounter OOM because of their own declarations, memory limits or behavior; there is no unconditional guarantee.

High density results from better node-resource utilization, not from pursuing instance count in isolation. Capacity must be validated against actual workload peak working sets, active fraction, recovery cost and node safety margin.

<a id="7-网络与多租边界"></a>
## 7. Networking and tenant boundaries

<a id="71-网络目的"></a>
### 7.1 Networking goals

`connector` provides these foundations for sandbox networking:

- fast allocation, restoration and reclamation of sandbox network resources;
- fast in-kernel eBPF/TC forwarding;
- isolation between sandboxes by default, without arbitrary direct port-to-port forwarding;
- platform-generated and verified trusted sandbox network identity, without trusting guest-provided identity;
- propagation of trusted identity to an external policy gateway;
- integration foundations for per-sandbox public/private network access, DNS, proxy and audit policies.

This overview defines those functional goals. Network-locator encoding, encapsulation fields and option layouts belong to connector's detailed documentation and are not the primary user interface.

<a id="72-功能状态"></a>
### 7.2 Capability status

| Capability | Status | Meaning |
| --- | --- | --- |
| Basic vSwitch, port lifecycle and sandbox isolation | Delivered | Maintained on connector main and covered by component E2E |
| Management-service path and external-gateway integration foundations | Delivered | vSwitch supplies trusted ingress/egress and identity-handoff foundations |
| Centralized policy gateway | Integration supported | The operator's external gateway implements public/private network, DNS, proxy and audit policy |
| Lightweight node-local Egress | Proposed | Only a design proposal in [`connector#9`](https://github.com/kuasar-sandbox/connector/issues/9), not a currently delivered vSwitch capability |
| OpenTelemetry | Proposed | Still tracked by [`kuasar-sandbox#52`](https://github.com/kuasar-sandbox/kuasar-sandbox/issues/52) and implementation work in progress; not a deployment prerequisite |

Delivered means merged into component main with component validation. It does not mean an aggregate Stable Release containing that code has already been published. An issue, RFC or PR alone does not turn a Proposed capability into a delivered one.

<a id="73-多租安全"></a>
### 7.3 Tenant security

- Platform identity credentials and content-protection keys have separate purposes. APISecret authenticates platform identity and control-plane operations; ManifestKey protects the content-key domain.
- Control-plane access, ordinary data-plane access and remote execution use credentials with different scopes. Remote-execution tokens bind a particular subject, purpose and conditions.
- Content-protection keys remain in trusted host data paths and are not injected into the guest.
- Local and remote artifacts can be encrypted and verified according to deployment policy. Key protection, backup and rotation remain operator responsibilities.
- Content-key and security-domain configuration determine deduplication and sharing scope. Identical content does not itself justify sharing across tenants.
- Stable Sandbox ID, credential binding and trusted network-identity boundaries remain valid after pause, resume and migration.
- Network isolation, node admission and fault domains combine with data encryption to establish tenant security; no single mechanism substitutes for all of them.

Credential updates affect only later business records that are created with copied credentials. Credentials already persisted in existing sandbox records retain their original binding; updating a key-distribution table does not automatically rebind those records.

<a id="8-集群与可靠性"></a>
## 8. Clustering and reliability

<a id="81-单节点和集群"></a>
### 8.1 Standalone and cluster operation

On a standalone node, `node-ctl conductor serve` manages local sandboxes, builds, data-plane proxying and the optional Reservation Controller. A cluster consists of three independent `cluster-ctl` roles:

- `registry`: maintains reliable execution state for nodes, routes and placers;
- `router`: provides the E2B-compatible unified entry point and routes by sandbox-group;
- `placer`: imports group configuration and selects candidate nodes, without owning sandbox lifecycle.

The target node and sandboxer still execute the lifecycle. The public Sandbox ID remains stable through same-node restoration, cross-node migration and replacement placement. The node-internal execution ID can change with the placement generation.

<a id="82-恢复与故障域"></a>
### 8.2 Recovery and fault domains

- `sandbox-ctl`, the VMM, node, registry, router and placer are independently observable and recoverable processes/fault domains.
- Durable node records and runtime Inventory support restart recovery and orphan cleanup. A host socket's existence alone does not prove guest readiness; recovery must be confirmed through an actual health check or execution operation.
- Local artifacts support node-affine recovery. Shared-file or Manifest artifacts support import and restoration independently of the original node.
- Registry stores cluster execution state; the node is authoritative for a running sandbox. Placer only recommends placement, and node admission still performs final resource confirmation.
- Group is the cluster partition key. Routing, placement, credential verification and operations remain group-scoped.

<a id="83-版本与验证"></a>
### 8.3 Versions and validation

Components have independent versions and releases. An aggregate version selects exactly six release units: four component archives, the guest runtime and the guest kernel. Aggregate preparation validates component assets and SHA256, then runs owner E2E and platform combination tests in a real KVM environment. The release package records the exact version combination to avoid mixing assets from different dates or aggregate versions.

These mechanisms support version pinning, fault diagnosis and repeatable validation in production. Operators still need production implementations and configuration for TLS, persistent backends, backups, monitoring, network policy and capacity.

<a id="9-性能与容量"></a>
## 9. Performance and capacity

This overview does not present one-off measurements or capacity projections as universal capabilities. Performance results are recorded in [perf.md](perf.md) and regressed through the owning components' E2E/performance entry points.

An externally comparable result must at least identify:

- the aggregate version and all relevant component versions or exact commits;
- CPU, memory, disk, network, KVM and host software environment;
- the local-file, shared-file or object-storage path;
- cache levels and cold/hot state;
- sandbox vCPU, memory, disks and workload;
- sample count, aggregation method, percentiles and failure rate;
- the start and end events defining latency or throughput;
- the raw report or reproducible test entry point.

Numbers without this context cannot support promises about node capacity, startup/restore latency, cache hit rate or storage savings. Design goals must be labeled as targets. Only validation at a specified version and environment establishes a result within that test's scope.

<a id="10-部署与发行状态"></a>
## 10. Deployment and release status

Kuasar Sandbox provides standalone and cluster topologies, node admission and recovery, independent processes and fault domains, component and aggregate versions, real MicroVM cross-component E2E, release-asset verification and exact version combinations.

The current Stable aggregate version is [`release-v0.1.2`](https://github.com/kuasar-sandbox/kuasar-sandbox/releases/tag/release-v0.1.2). Preview remains available for development and evaluation. Current GitHub releases provide prebuilt Linux x86_64 assets. Source builds support x86_64 and aarch64, but a supported source-build architecture is not automatically a published-asset architecture. [GitHub Releases](https://github.com/kuasar-sandbox/kuasar-sandbox/releases) and [release.md](release.md) are authoritative for the latest status.

## 11. See also

- [deployment.md](deployment.md) - topology, processes, ports and startup/shutdown dependencies
- [perf.md](perf.md) - component performance baselines and regression methods with environmental context
- [release.md](release.md) - component/aggregate versions, assets and publication transactions
- [Demo](../test/demo/DEMO.md) - local environment and E2B SDK demonstration
- [Full validation](../test/QUICKSTART.md) - complete aggregate-release validation
- [`orchestrator`](https://github.com/kuasar-sandbox/orchestrator/tree/main/docs) - node, resource and cluster design
- [`sandboxer`](https://github.com/kuasar-sandbox/sandboxer/tree/main/docs) - MicroVM, snapshot and guest coordination
- [`accelerator`](https://github.com/kuasar-sandbox/accelerator/tree/main/docs) - Manifest, store and cache
- [`connector`](https://github.com/kuasar-sandbox/connector/blob/main/docs/vswitch.md) - vSwitch implementation and networking details
- [`guest-runtime`](https://github.com/kuasar-sandbox/guest-runtime/tree/main/docs) - runtime, vmlinux and flattening
