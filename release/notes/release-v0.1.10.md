<!-- kuasar-release-notes {"version":"release-v0.1.10","aggregate_sha":"f30e9548ac8ec8ef629f45716717830a318375a0","components":{"accelerator":"v0.1.9","connector":"v0.1.6","sandboxer":"v0.1.10","orchestrator":"v0.1.10","runtime":"runtime-v0.1.10","vmlinux":"vmlinux-v0.1.5"}} -->
# Kuasar Sandbox release-v0.1.10

<!-- kuasar-release-notice -->
## Important upgrade notice / 重要升级提醒 <!-- docs:allow-han -->

> **[Sandboxer #378](https://github.com/kuasar-sandbox/sandboxer/issues/378) affects all previously released versions. All older deployments should upgrade to `release-v0.1.10` as soon as possible.**
>
> **本次修复的 [sandboxer #378](https://github.com/kuasar-sandbox/sandboxer/issues/378) 影响此前所有已发布版本，建议所有旧版部署尽快升级至 `release-v0.1.10`。** <!-- docs:allow-han -->

On-demand restore could expose stale parent bytes after guest memory release, including after capture without another access. This fixes CH release notification and Sandboxer capture: released pages become explicit zero; untouched pages inherit; rewritten pages keep current data.

**Upgrade Sandboxer and Cloud Hypervisor together**, including running old processes. Use the patched Cloud Hypervisor v51.1 bundled with the selected Sandboxer tag. The v51.1 version string alone does not identify the #378 fix. The fix cannot reconstruct release history already lost in older snapshots; validate important snapshots against expected application state.

**升级时必须一并替换包含修复的 Sandboxer 和 Cloud Hypervisor。** 正在运行的旧进程不会因磁盘文件替换而自动获得修复。该修复不会自动恢复旧快照中已经丢失的页面释放记录。 <!-- docs:allow-han -->

<!-- /kuasar-release-notice -->

## Highlights

Released-memory correctness, cumulative dirty-memory capture and local-history compaction, recoverable startup, sparse artifact handling, Store/Cache lifecycle improvements, and final-output/shared-ring fixes. Snapshot version 1 and complete-memory restore remain supported.

## Component versions

| Release unit | Version |
|---|---|
| `accelerator` | `v0.1.9` |
| `connector` | `v0.1.6` |
| `sandboxer` | `v0.1.10` |
| `orchestrator` | `v0.1.10` |
| `runtime` | `runtime-v0.1.10` |
| `vmlinux` | `vmlinux-v0.1.5` |

The six units version independently and are selected by exact Tag.

## Asset categories

16 assets: 12 component archives, platform, two workbench images, and `SHA256SUMS`. Verify hashes; extract native products and platform together; import workbench separately with `docker load -i`. Registry: `ghcr.io/kuasar-sandbox/workbench:v0.1.10`. The binding distinguishes archive, image and registry identities.

## Supported environment

Native Linux x86_64/aarch64; Host Linux minimum 5.10. Use the paired patched CH v51.1 supplied by the selected Sandboxer tag. System tests require cgroup v2, 4 KiB pages, KVM/TUN, UFFD/BPF and documented Docker prerequisites. Workbench privileged mode trusts administrators. Read actual cases and `qualification_scope` below: ARM `artifact-only` is not KVM/system execution.

## Quick Start

[Quick Start](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/release-v0.1.10/docs/quickstart.md).

## Production deployment

[Deployment prerequisites](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/release-v0.1.10/docs/deployment.md). Validate workloads and production security/recovery; prioritize the coordinated upgrade above.

## Known limitations

- Compatibility without the dirty API requires release-fixed CH; old notification-suppressing CH remains affected by #378.
- Release notification adds required synchronous processing; no universal speed/size gain is promised. Compaction may change parent identities.
- Disk limits remain v3: 1 TiB, v4: 32 GiB; old binaries cannot open new v3 disks above 32 GiB.
- Store objects remain revocable, not crash-atomic. Concurrent unlink/abort can cause actual absence, not visibility lag; real write/sync errors still fail.
- Final-output receipt is bounded best-effort after transport failure/timeout, not sink-fsync confirmation.
- Cache `disk_bytes` is a soft aging input, not a quota; abnormal exit can lose unflushed cache values (ordinary cache misses). Reclamation still depends on compaction/GC.
- Explicit deadlines/cancellation still apply. Demo credentials/TLS are evaluation-only; multi-node networking requires its infrastructure.

## Security

[Report vulnerabilities privately](https://github.com/kuasar-sandbox/kuasar-sandbox/security/advisories/new).

## Versioning and release channels

Stable, non-prerelease. Preview remains the development channel; historical Tags/assets remain immutable. Previous aggregate: `release-v0.1.9`.

## Component updates

### accelerator

Selected version: `v0.1.8` → `v0.1.9`.

Presence encoding through 1 TiB; bounded bitmap/skip work; private RocksDB wrapper with unchanged 9.7.4/data format; real peer Ping; request draining; named TCP/UDS socket activation. Legacy carriers remain readable. Cache frequency aging now also follows successful write turnover; rebuildable cache values skip WAL, while normal Close flushes data and reports errors. Filesystem Store successful uploads no longer require read-after-write confirmation (#209); validation, pre-upload dedup and real sync errors remain.

[Full comparison](https://github.com/kuasar-sandbox/accelerator/compare/v0.1.8...v0.1.9)

### connector

Selected version unchanged: `v0.1.6`.

### sandboxer

Selected version: `v0.1.9` → `v0.1.10`.

Fixes #378 in CH and capture; adds dirty-memory compaction, token-bound initial-launch confirmation, UFFD unmap wakeup, and sparse export/bitmap improvements. Final-output receipt is bounded (#381), and shared-ring index ordering is atomic (#383); this does not attribute every historical guest failure to #383.

[Full comparison](https://github.com/kuasar-sandbox/sandboxer/compare/v0.1.9...v0.1.10)

### orchestrator

Selected version: `v0.1.9` → `v0.1.10`.

Retains accepted startup through recoverable Manifest transport failures, removes the implicit bootstrap deadline, and fences obsolete idle runners; links selected dependencies.

[Full comparison](https://github.com/kuasar-sandbox/orchestrator/compare/v0.1.9...v0.1.10)

### runtime

Selected version: `runtime-v0.1.9` → `runtime-v0.1.10`.

Dependency rebuild including sandbox-init; guest-runtime source is unchanged, not a standalone feature update.

[Full comparison](https://github.com/kuasar-sandbox/guest-runtime/compare/runtime-v0.1.9...runtime-v0.1.10)

### vmlinux

Selected version unchanged: `vmlinux-v0.1.5`; no guest-kernel update.

Verify every archive with `SHA256SUMS`. GitHub supplies source archives automatically.
