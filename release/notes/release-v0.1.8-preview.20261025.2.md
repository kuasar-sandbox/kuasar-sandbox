<!-- kuasar-release-notes {"version":"release-v0.1.8-preview.20261025.2","aggregate_sha":"2584b4e1ba7a96f7491807c9d26fc5a22c490d82","components":{"accelerator":"v0.1.8-preview.20261025.1","connector":"v0.1.6","sandboxer":"v0.1.8-preview.20261025.2","orchestrator":"v0.1.8-preview.20261025.2","runtime":"runtime-v0.1.8-preview.20261025.2","vmlinux":"vmlinux-v0.1.4"}} -->
# Kuasar Sandbox release-v0.1.8-preview.20261025.2

Compared with the last complete Preview, `release-v0.1.8-preview.20261024`, this Preview combines persistent disk-presence tracking and guest discard support, more explicit image-conversion resource policy, and safer checkpoint and connection error handling. The component table and validation binding identify the exact combination delivered.

## Highlights

- **Guest discard without exposing lower-layer data.** Writable disks distinguish Hole, Zero and Data. Guest DISCARD can release upper payload while preserving a logical Zero that masks the immutable base. Checkpoint, export, restore and partial-write paths preserve that distinction. A matching tarstream carrier transports presence metadata and can omit full Zero pages while retaining the existing logical artifact identity.
- **Opt-in conversion quotas.** Zero-valued layer-metadata and planning quotas no longer impose the old fixed admission ceilings. Explicit positive quotas keep their conservative accounting. Checked representation bounds, decoder and archive limits, path checks and integrity validation remain in force; the execution environment still needs sufficient memory and storage.
- **Checkpoint error handling and bounded maintenance.** Template materialization publishes failed health before waking completion waiters. Checkpoint publication preserves read or packing errors and cleans temporary output instead of invoking a nil cleanup callback. Checkpoint deletion and bounded reclamation remain separate operations, with reader pins protecting retained data.
- **Standalone Connect waits for route acknowledgement.** Successful standalone Connect responses wait for current Proxy registrations to acknowledge preceding route updates, including imported targets. This fences route publication; it does not wait for VM or application readiness. An already accepted resume continues if the caller disconnects or the route barrier fails.

## Component versions

| Release unit | Version |
|---|---|
| `accelerator` | `v0.1.8-preview.20261025.1` |
| `connector` | `v0.1.6` |
| `sandboxer` | `v0.1.8-preview.20261025.2` |
| `orchestrator` | `v0.1.8-preview.20261025.2` |
| `runtime` | `runtime-v0.1.8-preview.20261025.2` |
| `vmlinux` | `vmlinux-v0.1.4` |

The aggregate selects six release units from five component repositories. The runtime and kernel are separate release units from guest-runtime; an unchanged kernel tag is not a claim that its source equals current guest-runtime main.

## Asset categories

Verify every explicit archive with `SHA256SUMS`. Extract native component archives and platform-release together. Import the separate Workbench archive with `docker load -i`; never extract it into products. The structured publication binding records the tested archive and registry identities.

## Supported environment

Prebuilt products and Workbench target native Linux x86_64 and aarch64. System tests require the documented cgroup v2, 4096-byte page, KVM/TUN, UFFD/BPF and Docker prerequisites. Workbench system mode uses trusted administrator privileged access, not hostile-root confinement.

Consult the final per-architecture records for executed cases and `qualification_scope`. Planned cases and ARM `artifact-only` Workbench qualification do not establish system, network, KVM lifecycle or snapshot/restore execution.

## Quick Start

Follow the [Quick Start](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/release-v0.1.8-preview.20261025.2/docs/quickstart.md) to verify the native assets, prepare a template, and exercise create, guest execution, pause, resume and cleanup.

## Production deployment

Review [Deployment](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/release-v0.1.8-preview.20261025.2/docs/deployment.md). Configure production TLS, durable storage, network policy, credentials, monitoring and recovery, and validate capacity with representative workloads.

## Known limitations

- **Export old working disks before upgrading.** Single-layer v3 and checkpoint v4 DIFF working files replace the earlier raw/v1/v2 working formats. Older working files and their local checkpoints are rejected without modification; there is no in-place migration. Export them with the old executable before upgrading. Existing portable tarstreams remain readable. Raw ext4 provisioning templates remain supported as `diff_template` inputs; they must not be supplied as an existing active `diff` file.
- Writable logical disk capacity must be positive, 4 KiB aligned and at most 32 GiB. This limit excludes immutable read-only devices and historical physical storage. Guest DISCARD handles complete pages; partial edge pages remain unchanged. Retained checkpoints can keep payload allocated, so a successful discard or deletion does not promise immediate host-space reclamation. This does not add WRITE_ZEROES or automatic compression of ordinary zero-filled writes.
- Presence-aware tarstreams require matching readers. The release's linked consumers must use the selected Accelerator library. Omitting Zero payload saves transport and allocation, but hashing Zero still costs CPU proportional to its logical length. No general throughput improvement is claimed.
- Removing default implicit conversion quotas does not guarantee arbitrary input will complete or bound total process RSS. Standalone callers must bound their execution environment or supply explicit quotas. Positive quotas retain cumulative conservative accounting; the separate retained-state accounting proposal [Accelerator #184](https://github.com/kuasar-sandbox/accelerator/pull/184) is not included in this release.
- Standalone Connect can return 503 when the route barrier fails without rolling back an accepted resume or committed timeout update. Its success does not guarantee guest readiness, nor does it change the cluster CmdConnect ownership protocol.
- Quick Start self-signed certificates, the local registry and demo credentials are for evaluation. Multi-node deployments and centralized network policy require their documented supporting infrastructure.

## Security

[Report vulnerabilities privately](https://github.com/kuasar-sandbox/kuasar-sandbox/security/advisories/new). Do not disclose unpatched vulnerabilities in public issues.

## Versioning and release channels

This is a Preview prerelease for development and evaluation. It does not replace Stable v0.1.7.

Previous aggregate selection: `release-v0.1.8-preview.20261024`.

## Component updates

### accelerator

Selected version: `v0.1.7` → `v0.1.8-preview.20261025.1`.

Page-presence metadata preserves the distinction between absent lower-layer content and explicit Zero while retaining the existing logical identity algorithm. The implicit layer, plan and source-index admission ceilings are now opt-in; overflow, format and integrity checks remain. This includes the compatibility change in #187, not the unmerged accounting redesign in #184.

[Full comparison](https://github.com/kuasar-sandbox/accelerator/compare/v0.1.7...v0.1.8-preview.20261025.1)

### connector

Selected version: `v0.1.6`. The source remains `8fc4b6a266ff551b2747a83f43ce1c8a717ef187`; this selection introduces no Connector networking implementation change.

### sandboxer

Selected version: `v0.1.8-preview.20261024` → `v0.1.8-preview.20261025.2`.

Writable v3/v4 storage persists logical presence and supports guest discard without exposing the base. Bounded GC and directory maintenance reclaim eligible checkpoint data while respecting readers. Materialization completion records failure before waking waiters. The cleanup fix in [#338](https://github.com/kuasar-sandbox/sandboxer/pull/338) preserves the original checkpoint read/packing error and removes intermediate output; the previously disclosed nil-callback panic is fixed in this release. Telemetry fixtures now obtain the artifact identity through the canonical tarstream parser, including sparse and presence-aware carriers, instead of scanning physical tar blocks as ordinary entries.

[Full comparison](https://github.com/kuasar-sandbox/sandboxer/compare/v0.1.8-preview.20261024...v0.1.8-preview.20261025.2)

### orchestrator

Selected version: `v0.1.8-preview.20261024` → `v0.1.8-preview.20261025.2`.

Standalone Connect waits on the existing ordered Proxy route barrier for Running, Starting and imported targets. Caller cancellation and barrier failure do not cancel an already accepted resume. Existing managed checkpoint ownership and the #481 unsupported-cleanup fix remain in this source. Resource qualification keeps the raw ext4 provisioning image immutable and gives the product a separate active DIFF path, matching the new writable-format contract.

[Full comparison](https://github.com/kuasar-sandbox/orchestrator/compare/v0.1.8-preview.20261024...v0.1.8-preview.20261025.2)

### runtime

Selected version: `runtime-v0.1.8-preview.20261024` → `runtime-v0.1.8-preview.20261025.2`.

Runtime tests retain exact legacy and presence-carrier golden identities. The flatten guide documents zero-valued optional quotas and unchanged explicit-quota accounting. The linked CLI behavior and disk/runtime integration depend on the selected Accelerator and Sandboxer inputs, so source documentation alone is not evidence of rollout.

[Full comparison](https://github.com/kuasar-sandbox/guest-runtime/compare/runtime-v0.1.8-preview.20261024...runtime-v0.1.8-preview.20261025.2)

### vmlinux

Selected version: `vmlinux-v0.1.4`. The kernel is reused unchanged. Its independent tag source remains authoritative and is distinct from current guest-runtime main.

## Platform and release tooling

Source qualification rebuilds linked tarstream readers with a selected carrier change and rebuilds node management when Sandboxer storage-library inputs change. Resource and telemetry consumer cases are selected when their storage or tarstream inputs change, with exact candidate and companion product/test identities. Ordinary PRs take non-candidate inputs from the current complete validated aggregate, or its declared complete predecessor while a new aggregate is pending; separately published components from an unfinished aggregate are not overlaid onto that baseline. The legacy capability-migration checks remain in place. Bounded failure diagnostics preserve selected launch and resource-state evidence without changing test assertions or timeouts. These PR checks do not replace this release's exact-assets aggregate qualification.

Release prose remains separately bound to the exact aggregate source and six component versions. The publisher budgets complete machine evidence and registry proof before adapting narrative, retaining full reviewed prose at an immutable source link when necessary.

Verify every explicit archive with `SHA256SUMS`. GitHub supplies source archives automatically for the tag.
