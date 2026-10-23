<!-- kuasar-release-notes {"version":"release-v0.1.8-preview.20261023","aggregate_sha":"3e5bbf1850e5365e737837c98c860dbf5f648684","components":{"accelerator":"v0.1.7","connector":"v0.1.6","sandboxer":"v0.1.8-preview.20261023","orchestrator":"v0.1.8-preview.20261023","runtime":"runtime-v0.1.8-preview.20261023","vmlinux":"vmlinux-v0.1.4"}} -->
# Kuasar Sandbox release-v0.1.8-preview.20261023

This first Preview on the v0.1.8 line builds on Stable v0.1.7. It brings checkpoint-backed disk lifecycles to Orchestrator-managed business sandboxes, consolidates writable storage in Sandboxer, and improves artifact acquisition for CI and releases.

## Highlights

- **Managed disk checkpoints.** New business sandboxes launched by Orchestrator enable checkpoint-capable writable disks. Pause creates a named disk version that can be reused for restore and portable publication. Consumed versions are released after the sandbox is running and any publisher has finished using the source. Build A/B/C continue using single-layer writable disks and can materialize checkpoint-backed inputs.
- **Focused storage improvements.** Sandboxer batches physically contiguous active DIFF reads instead of issuing a separate direct read for each 4 KiB page. Single-layer raw/v1 and checkpoint-capable v2 storage now share the public `pkg/diffstore.Disk` frontend, separating writable storage from vhost transport. The read optimization targets this specific access pattern; it is not a general throughput or write-performance claim.
- **More reliable artifact acquisition.** Bounded retries can resume a partially downloaded Actions artifact after a network interruption or deadline, retaining its frozen identity and checking the complete size and SHA256 before extraction. An optional host-local cache coalesces concurrent requests for the same verified archive; each client still obtains authorized metadata and checks the archive digest. Hosts without the cache retain direct downloads.

## Component versions

| Release unit | Version |
|---|---|
| `accelerator` | `v0.1.7` |
| `connector` | `v0.1.6` |
| `sandboxer` | `v0.1.8-preview.20261023` |
| `orchestrator` | `v0.1.8-preview.20261023` |
| `runtime` | `runtime-v0.1.8-preview.20261023` |
| `vmlinux` | `vmlinux-v0.1.4` |

The aggregate pins six release units from five component repositories. `guest-runtime` supplies both the runtime and kernel units.

## Asset categories

Verify every explicit asset with `SHA256SUMS`. Extract the six native component archives and the matching platform-release archive together. Platform-release supplies the guides, Demo, canonical tests, helpers and launcher. Import the separate workbench image archive with `docker load -i`; never extract it into products. The publication binding records the tested archives and registry identities.

## Supported environment

Prebuilt products and Workbench target native Linux x86_64 and aarch64. System tests require cgroup v2, 4096-byte pages, KVM/TUN, UFFD/BPF and the documented Docker host prerequisites. Ordinary UID builds need no KVM or added capabilities. Workbench system mode uses trusted administrator privileged access, not hostile-root confinement.

Consult each architecture's validation record for executed cases and `qualification_scope`. ARM `artifact-only` Workbench qualification and planned cases do not establish system, network, KVM lifecycle or snapshot/restore execution.

## Quick Start

Follow the [Quick Start](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/release-v0.1.8-preview.20261023/docs/quickstart.md) to verify and install the native assets, prepare a template, and exercise create, guest execution, pause, resume and cleanup.

## Production deployment

Review [Deployment](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/release-v0.1.8-preview.20261023/docs/deployment.md). Configure production TLS, durable storage, network policy, credentials, monitoring and recovery for your topology, and validate capacity with representative workloads.

## Known limitations

- The Sandboxer storage refactor changes Go imports and exported storage types. It does not convert existing raw/v1/v2 files or change their on-disk formats. Downstream Go callers using the former vhost storage APIs must migrate to `pkg/diffstore`.
- Orchestrator now enables checkpoints for newly launched business sandboxes; standalone Sandboxer still requires explicit checkpoint enablement. Versioned DIFF uses a local Linux storage format that older readers cannot open. Retained versions consume space until deleted and reclaimed after readers release them. Read/write overhead remains workload-dependent, and ordinary export can still require full scans.
- Materializing a selected disk version during checkpoint publication or restore/cold start can panic on a read or packing error, including insufficient temporary-storage space. This masks the original error and can leave intermediate files behind ([Sandboxer #337](https://github.com/kuasar-sandbox/sandboxer/issues/337)); the fix in [#338](https://github.com/kuasar-sandbox/sandboxer/pull/338) is not included.
- Large or many-entry image conversions can still exceed metadata or source-selection budgets ([Accelerator #183](https://github.com/kuasar-sandbox/accelerator/issues/183)); the fix in [#184](https://github.com/kuasar-sandbox/accelerator/pull/184) is not included.
- The Quick Start self-signed certificates, local registry and demo credentials are for evaluation only. Multi-node deployment and centralized network policy require their documented supporting infrastructure.

## Security

[Report vulnerabilities privately](https://github.com/kuasar-sandbox/kuasar-sandbox/security/advisories/new). Do not disclose unpatched vulnerabilities in public issues.

## Versioning and release channels

This is a Preview prerelease for development and evaluation. It does not replace Stable v0.1.7.

Previous aggregate selection: `release-v0.1.7`.

## Component updates

### accelerator

Selected version unchanged: `v0.1.7`. The image-conversion implementation is unchanged from the preceding Stable selection.

### connector

Selected version unchanged: `v0.1.6`. No networking implementation update is introduced by this selection.

### sandboxer

Selected version: `v0.1.7` → `v0.1.8-preview.20261023`.

Active DIFF reads combine physically contiguous payload pages in bounded batches while preserving logical encryption-sector addressing and complete-page progress on short I/O. Writable single-layer and versioned disks share cache, lifetime and materialization handling through `pkg/diffstore`; vhost retains its transport role. Standalone CLI/configuration and format defaults remain unchanged by this storage refactor.

[Full comparison](https://github.com/kuasar-sandbox/sandboxer/compare/v0.1.7...v0.1.8-preview.20261023)

### orchestrator

Selected version: `v0.1.7` → `v0.1.8-preview.20261023`.

Checkpoint capture, publication and cleanup coordinate source ownership across resume and recovery. Cleanup retries retain the exact selected disk references and cannot roll back an already committed running sandbox or publication result. An explicit structured `checkpoint_unsupported` response to listing now ends that cleanup attempt without issuing deletes; other errors still retry, and later independent triggers recheck capability. This includes the fix for [#480](https://github.com/kuasar-sandbox/orchestrator/issues/480) in [#481](https://github.com/kuasar-sandbox/orchestrator/pull/481).

[Full comparison](https://github.com/kuasar-sandbox/orchestrator/compare/v0.1.7...v0.1.8-preview.20261023)

### runtime

Selected version: `runtime-v0.1.7` → `runtime-v0.1.8-preview.20261023`.

The runtime's own source remains unchanged; the bundle is rebuilt against the selected Sandboxer dependency containing the storage changes.

[Full comparison](https://github.com/kuasar-sandbox/guest-runtime/compare/runtime-v0.1.7...runtime-v0.1.8-preview.20261023)

### vmlinux

Selected version unchanged: `vmlinux-v0.1.4`. No kernel update is included.

## Platform and release tooling

Release notes support a reviewed summary bound to the exact aggregate source and component selection. The publisher derives its prose budget from the complete validation evidence and reserved registry proof; when needed, it retains the full reviewed text at an immutable source link instead of truncating evidence. Artifact download resumption and the optional verified host cache improve acquisition reliability without changing the product qualification requirements.

[Platform changes through the artifact-cache update](https://github.com/kuasar-sandbox/kuasar-sandbox/compare/release-v0.1.7...4c49e21dd998a989d652b884ad73b9191bdcd3c7).

Verify every explicit asset with `SHA256SUMS`. GitHub supplies source archives automatically for the tag.
