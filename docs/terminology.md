[English](terminology.md) | [简体中文](terminology_zh.md)

# Documentation terminology

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
