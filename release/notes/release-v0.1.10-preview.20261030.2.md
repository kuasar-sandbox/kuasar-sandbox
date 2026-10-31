<!-- kuasar-release-notes {"version":"release-v0.1.10-preview.20261030.2","aggregate_sha":"e840001801ccfb55f7f5b3821c43cff002d69e6a","components":{"accelerator":"v0.1.9-preview.20261030.1","connector":"v0.1.6","sandboxer":"v0.1.10-preview.20261030.2","orchestrator":"v0.1.10-preview.20261030.1","runtime":"runtime-v0.1.10-preview.20261030.2","vmlinux":"vmlinux-v0.1.5"}} -->
# Kuasar Sandbox release-v0.1.10-preview.20261030.2

<!-- kuasar-release-notice -->
## Important upgrade notice

**Includes the [Sandboxer #378](https://github.com/kuasar-sandbox/sandboxer/issues/378) released-memory fix. Replace Sandboxer and Cloud Hypervisor together, including old running processes.** Released pages must not regain stale parent-snapshot bytes; lost release history in older snapshots cannot be reconstructed. This is a Preview, not the separately qualified Stable release.
<!-- /kuasar-release-notice -->

## Highlights

Released-page capture correctness, dirty-memory compaction, recoverable startup, cache write-turnover aging, and bounded receipt of final app output before guest power-off. Snapshot version 1 and complete-memory restore remain supported.

## Component versions

| Release unit | Version |
|---|---|
| `accelerator` | `v0.1.9-preview.20261030.1` |
| `connector` | `v0.1.6` |
| `sandboxer` | `v0.1.10-preview.20261030.2` |
| `orchestrator` | `v0.1.10-preview.20261030.1` |
| `runtime` | `runtime-v0.1.10-preview.20261030.2` |
| `vmlinux` | `vmlinux-v0.1.5` |

## Asset categories

16 assets: 12 component archives, platform, two native Workbench images, and `SHA256SUMS`. Verify hashes; extract native products and platform together. Import Workbench separately with `docker load -i`. Registry: `ghcr.io/kuasar-sandbox/workbench:v0.1.10-preview.20261030.2`.

## Supported environment

Native Linux x86_64/aarch64. System execution needs cgroup v2, 4 KiB pages, KVM/TUN, UFFD/BPF and documented Docker prerequisites. Workbench privileged mode trusts administrators. Read executed cases and `qualification_scope` below: ARM `artifact-only` Workbench qualification is not KVM/system execution; separate native ARM results retain their own scope.

## Quick Start

[Quick Start](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/release-v0.1.10-preview.20261030.2/docs/quickstart.md).

## Production deployment

[Deployment prerequisites](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/release-v0.1.10-preview.20261030.2/docs/deployment.md). Validate workloads and recovery; this Preview does not replace Stable.

## Known limitations

- Old notification-suppressing CH remains affected by #378; replacing only Sandboxer is insufficient.
- Cache `disk_bytes` is soft aging, not a quota; abnormal exit may lose unflushed cache values. Reclamation depends on compaction/GC.
- Output shutdown is bounded best-effort after transport failure/timeout, not sink-fsync confirmation.
- Disk bounds: v3 1 TiB, v4 32 GiB; old binaries cannot open new v3 disks above 32 GiB. Demo credentials/TLS are evaluation-only.

## Security

[Report vulnerabilities privately](https://github.com/kuasar-sandbox/kuasar-sandbox/security/advisories/new).

## Versioning and release channels

Development Preview prerelease. Stable remains unchanged; historical tags/assets are immutable.

## Component updates

### accelerator

Reuses `.20261030.1`: presence encoding through 1 TiB, bounded skip work, Store lifecycle/socket activation, cache write-turnover aging and rebuildable-data WAL removal; normal Close flushes data and reports errors.

### connector

Unchanged `v0.1.6`.

### sandboxer

`.20261030.2` adds a bounded host-MUX receipt barrier for final app output before power-off (#381). Includes the paired CH/capture #378 fix, dirty-memory compaction and launch confirmation.

### orchestrator

Reuses `.20261030.1`: recoverable startup and corrected snapshot-compaction assertions. Original dependency binding remains unchanged; selected linked-input equivalence was verified.

### runtime

`.20261030.2` rebuilds dependencies including fixed sandbox-init; guest-runtime source is unchanged.

### vmlinux

Unchanged `vmlinux-v0.1.5`; no guest-kernel update.
