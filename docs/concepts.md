[English](concepts.md) | [简体中文](concepts_zh.md)

# Platform concepts and terminology

Use this guide to understand platform objects, instance identity and retained state. Continue with [sandboxes](sandboxes.md) and [templates](templates.md) for complete tasks; component specifications own exact fields and encodings.

## User scenarios

Kuasar Sandbox addresses these shared needs:

- Agents and Code Interpreters need independent kernel and filesystem state for each session, while avoiding complete initialization from an empty environment every time.
- Serverless and parallel tasks need multiple instances with independent identities created from one prepared environment.
- Reinforcement-learning rollouts need parallel copies of a common initial environment, with each trajectory evolving independently.
- Long sessions wait for model calls, human confirmation or external events. Idle resources should be released during those waits; an instance can be paused when appropriate and later continue the same logical session.
- Both standalone and cluster deployments need explicit boundaries for admission, recovery, network identity and data portability.

The system's capabilities support production deployment. Stable and Preview describe the release stability of public assets and interfaces; they do not replace production configuration of capacity, durable storage, TLS, network policy and credentials by the operator.

## Objects and identities

| Object | What it means to a platform user | Authoritative contract |
| --- | --- | --- |
| Template | Reusable published input from which new independent instances are created | [Template workflow](templates.md) and [Build](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-build.md) |
| Sandbox | One managed logical instance, with an owner, stable public identity and retained state | [Managed lifecycle](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/sandbox-lifecycle.md) |
| Run | One execution attempt of that instance; its RunID and node-local resources can change | Managed lifecycle; [Runtime](https://github.com/kuasar-sandbox/sandboxer/blob/main/docs/runtime.md) |
| Image | An application filesystem and launch configuration, before a sandbox captures its own disk state | [Image preparation](https://github.com/kuasar-sandbox/accelerator/blob/main/docs/image-preparation.md) |
| Sandbox E | Portable workload configuration and disk graph; no running process memory | [Sandbox artifacts](https://github.com/kuasar-sandbox/sandboxer/blob/main/docs/artifacts.md) |
| Snapshot S | Captured VM memory/state plus a reference to E | Sandbox artifacts |
| Carrier | A file, Bundle or Manifest representation that transports content | [Generic artifacts](https://github.com/kuasar-sandbox/accelerator/blob/main/docs/artifacts.md) |

A successful control response can accept a launch before the application is ready.
Check application behavior through the data path. A dead instance, an E-only paused
instance and an S-backed paused instance have different recovery choices; their
exact transitions belong to the managed lifecycle contract.

## Snapshot and instance semantics

### Creating new instances from a snapshot template: 1:N

A template completes environment, dependency and optional application-process initialization before publication. Multiple creation requests can reference the same read-only template parent, but each instance receives its own Sandbox ID, network identity, credential binding and incremental state. Subsequent writes, process progress and pauses belong only to that instance and do not modify the template or other instances.

This serves Agent, Code Interpreter, RL rollout, Serverless and parallel-task workloads: the template bears the common initialization cost, and each instance evolves independently from the same starting point.

### Pausing and resuming the same instance: 1:1

A pause that captures memory preserves the process, memory and filesystem state of the same logical instance. Explicit pause defaults to this snapshot behavior. With `memory: false`, pause preserves disk artifacts and later starts the application again; it does not restore the previous process memory. `autoPauseMemory` independently selects the capture behavior of automatic pause. Connect/resume retains the stable public Sandbox ID in either case. Artifacts kept on the original node allow in-place recovery; publication to a named shared-file location or Manifest makes the data reachable elsewhere. Cross-node import/resume additionally requires preserved ownership, the applicable migration protocol (including a valid MigrationToken where required), compatible destination inputs and target admission; follow [restore eligibility](upgrading.md#restore-eligibility). The node-internal execution ID can change while the public identity remains stable.

This serves waits for model calls, human confirmation or external events, long sessions and node migration. A `warm pool` is an upper-level resource-preparation policy that can keep instances or templates available in advance; it is not an independent snapshot technology.

### Layered snapshots

Snapshot reuse comes from explicit parent relationships:

```mermaid
flowchart TD
  T["Read-only template parent"] --> A["Instance A delta"]
  T --> B["Instance B delta"]
  A --> A2["Later pause A"]
  B --> B2["Later pause B"]
```

- Instances created from the same template share the template parent and maintain their own deltas.
- A later pause can capture current disk and memory state as a new layer with an explicit reference to the previous parent.
- Disk artifacts and memory snapshots record their origins separately; restoration validates the complete parent chains.
- Local files, named shared files and Manifest carry the same layering semantics. The carrier does not change the user identity model.

Sharing efficiency does not depend on different running VMs coincidentally producing identical memory bytes. Reusable content comes from explicit template parents, the host page cache for immutable runtime/rootfs content and content organization of stable artifacts within a security domain.

## Unified reference semantics

The upper-level lifecycle uses common artifact references and integrity information rather than requiring all data to be converted to one backend. The supported paths include:

1. **Local files:** suitable for standalone nodes, local NVMe and node-affine workloads. Images, disk layers and snapshots can retain their native file representation.
2. **Shared file storage:** NAS, NFS or another shared filesystem provides native file access across nodes. A named file location can hold snapshots and images directly, without first converting them to content chunks.
3. **Manifest / object storage / tiered cache:** suitable for large-scale distribution, remote persistence, cross-node recovery, on-demand reads and hot-content caching. Artifacts do not depend on files on the original node; the store backend can be a filesystem or S3-compatible object storage.

Operators can select different paths for different artifacts. For example, temporary node-local state can use NVMe, shared NAS snapshots can use file locations directly, and widely distributed images can use Manifest and tiered caching.

## Sparse and on-demand data

Sparse semantics have three states: Hole comes from authoritative metadata, Zero denotes logical zero data and Data denotes actual content. The system does not scan zero bytes to invent Hole regions. Snapshots and disk layers can be read by reference range. Cache hits and prefetch reduce remote access without changing artifact integrity or parent relationships.

<a id="glossary"></a>
## Glossary

This glossary aligns documentation wording; it does not introduce a new API or replace component specifications. Exact code identifiers remain unchanged.

| Term | Documentation meaning |
| --- | --- |
| Kuasar Sandbox | The overall project, comprising one project repository and five independently evolving component repositories. |
| Project repository | `kuasar-sandbox/kuasar-sandbox`: system-level documentation, integration validation and aggregate releases. |
| Component | `orchestrator`, `sandboxer`, `accelerator`, `connector` or `guest-runtime`; not synonymous with a release unit or daemon. |
| Release unit | One independently versioned artifact line. `guest-runtime` owns runtime and vmlinux lines, yielding six component release units. |
| Aggregate release | An explicitly selected, jointly validated combination of published release units; its version need not match theirs. |
| Snapshot-template instantiation | Create independent instances from a prepared template (1:N). |
| Stateful pause and resume | Save and resume the same logical instance (1:1); distinguish memory restore from explicit cold start. |
| Stable identity | The external logical sandbox identity preserved across supported node-local replacement or migration. Preserve the precise `StableID`, `NodeSandboxID` or other field name in each protocol. |
| Parent layer | Explicitly referenced shared base data; not a claim of identical memory across independently booted VMs. |
| Incremental state | Changes owned by an instance relative to its referenced base. |
| Artifact / carrier | Logical workload content versus its physical representation. Use the owning specification's E/S, image/overlay and carrier definitions. |
| Data path | Local files, named shared-file locations, or Manifest/object-store/cache access as selected by the deployment and artifact reference. |
| On-demand loading | Load the memory pages or disk blocks actually accessed; not an assertion that full artifacts are always downloaded. |
| Prefetch | The existing working-set loading optimization; do not reuse the term for unrelated full-artifact downloads. |
| High-density resource governance | Reclaim and reuse idle capacity while protecting admitted active work; density is a result of improved node utilization, not an unconditional instance count. |
| Capacity | The VM's configured maximum memory; not identical to its current physical charge or node reservation. |
| Headroom | The policy-defined guest memory margin. Use the exact `resources.allocatable.memory` semantics in the owning specification. |
| Node reservation | Node-accounted resources granted to a sandbox; distinct from sandbox-local balloon/cgroup execution and diagnostic host usage. |
| Conductor / Proxy | Lifecycle/control-plane authority versus the independent node data-plane role; describe current deployment modes from the owning source. |
| Registry / Router / Placer | Cluster state, unified ingress/routing, and placement roles; do not assign node lifecycle execution to the placer. |
| Trusted sandbox network identity | Identity established by the host network infrastructure rather than trusting guest-supplied addresses. |
| Policy gateway | An external or separately deployed policy execution plane; network integration infrastructure does not imply every gateway feature is implemented. |
| Security domain | The configured scope of permitted sharing and protection; not unconditional sharing across all tenants. |
| Stable / Preview / Proposed | Public release channel status and proposed-design status are separate from production deployment capability. A proposal is not a delivered feature. |

Use MUST/MUST NOT/SHOULD/SHOULD NOT/MAY only with the strength established by the source requirement. Keep technical detail in design specifications; use capability-oriented language in overview and promotional material.
