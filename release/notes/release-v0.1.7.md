<!-- kuasar-release-notes {"version":"release-v0.1.7","aggregate_sha":"921c7618e8d981e63ed6d4a2a2a0d5eea246e640","components":{"accelerator":"v0.1.7","connector":"v0.1.6","sandboxer":"v0.1.7","orchestrator":"v0.1.7","runtime":"runtime-v0.1.7","vmlinux":"vmlinux-v0.1.4"}} -->
# Kuasar Sandbox release-v0.1.7

Kuasar Sandbox is a production-deployable MicroVM sandbox platform for large-scale agent, serverless, and reinforcement-learning workloads. This aggregate contains selected user guides, Demo and canonical tests, the original component archives, and both native workbench images under one aggregate version. The declared native x86_64 profile uses the current full ordinary case selection. The hosted ARM profile requires native non-KVM image/storage E2E and `artifact-only` Workbench qualification; planned Workbench cases do not establish network, KVM lifecycle or snapshot/restore execution. Consult the per-architecture validation records for executed cases and `qualification_scope`, including any `native-full` acceptance. Historical releases retain their original coverage.

## Highlights

This release focuses on image-conversion correctness, opt-in local disk checkpoints, and more predictable Proxy operation and recovery. Image preparation and the runtime now use the linked Go EROFS generator. Sandboxer adds explicit versioned disk checkpoints, while Proxy adds socket activation, graceful draining and in-place restart. The component sections below explain the behavior and its limits.

## Component versions

| Release unit | Version |
|---|---|
| `accelerator` | `v0.1.7` |
| `connector` | `v0.1.6` |
| `sandboxer` | `v0.1.7` |
| `orchestrator` | `v0.1.7` |
| `runtime` | `runtime-v0.1.7` |
| `vmlinux` | `vmlinux-v0.1.4` |

The five component repositories version and publish independently. `guest-runtime` supplies the `runtime` and `vmlinux` release units but remains one component repository. This aggregate pins all six units as one tested platform combination.

## Asset categories

| Category | Contents | Use |
|---|---|---|
| Products | Six original upstream archives per architecture | Extract only the selected native architecture |
| platform-release | Bilingual guides, Demo, canonical tests, helpers, locked wheels and launcher | Extract beside products |
| workbench | One gzip-compressed Docker image archive per architecture | Import separately with `docker load -i`; never extract into products |
| SHA256SUMS | All declared archive hashes | Verify downloaded bytes before use |

The same tested images are available at `ghcr.io/kuasar-sandbox/workbench:v0.1.7`. Each compressed workbench archive is checked to be smaller than 2 GiB. The validation binding records its archive hash, image ID, registry identities, actual sizes, import/start timings and observed peak disk usage separately.

## Supported environment

Prebuilt products and workbench target native Linux x86_64 and aarch64. System tests require cgroup v2, 4096-byte pages, KVM/TUN, UFFD/BPF and the documented native Docker host prerequisites. Ordinary UID builds need no KVM or added capabilities. Workbench system mode is a trusted administrator environment with Docker privileged access; no custom host security-policy tools are required. Private namespaces/data and owned cleanup do not provide hostile-root confinement.

## Quick Start

Follow the [Quick Start](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/release-v0.1.7/docs/quickstart.md) to select the native product/platform assets from this complete aggregate, optionally import workbench, verify `SHA256SUMS`, prepare a template, and exercise create, guest exec, pause, reconnect/resume, kill, and cleanup with the unmodified E2B Python SDK.

## Production deployment

The system supports production deployment. Operators should complete workload-specific capacity validation and configure production TLS, durable storage, network policy, credentials, monitoring, and recovery for their topology. See [Deployment](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/release-v0.1.7/docs/deployment.md).

## Known limitations

- The new image generator retains the documented no-xattr policy. Existing artifacts remain readable, but output bytes may differ between generators. Treat generated artifacts and their source/generator identity as a matched set when reproducing or rolling back a build.
- Versioned DIFF checkpoints use an opt-in local Linux storage format that older readers cannot open. Retained checkpoints consume space until explicitly deleted and reclaimed after readers release them. Read/write overhead is workload-dependent; test representative workloads before enabling this mode. Lazy metadata loading does not eliminate full scans needed by ordinary export.
- Checkpoint publication and restore/cold-start paths that materialize a selected disk version can panic if materialization encounters a read or packing error, including insufficient temporary-storage space. The panic masks the original error and can leave intermediate files behind ([Sandboxer #337](https://github.com/kuasar-sandbox/sandboxer/issues/337)); the fix in [#338](https://github.com/kuasar-sandbox/sandboxer/pull/338) is not included.
- Some large or many-entry image conversions can still exceed the default metadata or source-selection budgets. [Accelerator #183](https://github.com/kuasar-sandbox/accelerator/issues/183) remains a known limitation in this frozen release; its later fix is not included.
- Multi-node clusters and centralized network policy require their documented supporting infrastructure.
- The Quick Start self-signed certificates, local registry, and demo credentials are for evaluation only.
- Node-local lightweight Egress and OpenTelemetry remain Proposed and are not release prerequisites.

## Security

Report vulnerabilities privately through [Report a vulnerability](https://github.com/kuasar-sandbox/kuasar-sandbox/security/advisories/new). Do not disclose unpatched vulnerabilities in public issues.

## Versioning and release channels

This aggregate is a Stable, non-prerelease release. Preview continues as the development and evaluation channel. Production readiness and public release-channel stability are separate dimensions.

Previous aggregate selection: `release-v0.1.6`.

## Component updates

### accelerator

Selected version: `v0.1.6` → `v0.1.7`.

Image conversion uses the linked Go EROFS generator instead of launching `mkfs.erofs`. Conversion verifies layer content and selected sources, streams bounded plans, and validates sources before completing artifact output. These changes improve failure handling and deployment consistency; they are not a blanket conversion-performance claim.

[Full comparison](https://github.com/kuasar-sandbox/accelerator/compare/v0.1.6...v0.1.7)

### connector

Selected version: `v0.1.5` → `v0.1.6`.

This update adds E2E capability declarations, not a new networking behavior.

[Full comparison](https://github.com/kuasar-sandbox/connector/compare/v0.1.5...v0.1.6)

### sandboxer

Selected version: `v0.1.6` → `v0.1.7`.

Sandboxer can retain named versions of root and data disks without copying all historical disk payload for each checkpoint. This is opt-in through `--enable-checkpoint`; named capture requires `snapshot/export --checkpoint ID`. Restore preparation loads versioned disk metadata on demand and releases temporary reader pins once preparation has handed over ownership. Ordinary capture and export remain available.

Sandboxer retires a MUX session after a partial frame-write failure so later control frames are not appended to a damaged stream and blocked waiters can be released. Vsock and usage-channel memory-pressure retries are bounded and retain their existing close/deadline behavior.

[Full comparison](https://github.com/kuasar-sandbox/sandboxer/compare/v0.1.6...v0.1.7)

### orchestrator

Selected version: `v0.1.6` → `v0.1.7`.

Proxy supports named socket activation, graceful draining, and in-place restart while preserving the original main PID. Restart pins the replacement image and its input, and supports recovery when replacement fails. Configure a positive shutdown timeout for in-place restart. Draining is bounded by that timeout, not a promise that every long-lived connection or multi-request workflow survives; external supervision remains necessary for crashed or hung processes.

Accepted builds can recover their routes after a temporary node endpoint lookup failure, instead of remaining stuck behind a cached empty endpoint. Image preparation is aligned with the linked Go EROFS conversion policy.

[Full comparison](https://github.com/kuasar-sandbox/orchestrator/compare/v0.1.6...v0.1.7)

### runtime

Selected version: `runtime-v0.1.6` → `runtime-v0.1.7`.

The runtime's `flatten-ctl` uses the linked Go EROFS generator, and the runtime bundle no longer needs the old EROFS executable. Incomplete chunk reads are rejected before hashing; CLI output paths use verified streaming plans from stable sources.

[Full comparison](https://github.com/kuasar-sandbox/guest-runtime/compare/runtime-v0.1.6...runtime-v0.1.7)

### vmlinux

Selected version unchanged: `vmlinux-v0.1.4`. This release does not change the kernel version.

[Platform and release tooling changes](https://github.com/kuasar-sandbox/kuasar-sandbox/compare/387fba8d1e352843b55ac90d5d22569d11450bf7...921c7618e8d981e63ed6d4a2a2a0d5eea246e640)

Verify every explicit asset with `SHA256SUMS`. GitHub provides source archives automatically for this tag.
