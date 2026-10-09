[English](perf.md) | [简体中文](perf_zh.md)

# perf - Performance validation and tuning

This document defines measurement boundaries, entry points, evidence requirements and regression methods for Kuasar Sandbox performance testing. Results apply only to the recorded versions, hardware, data path, cache state, sandbox specification and workload. A development-environment measurement cannot be extrapolated to all production deployments.

Historical absolute numbers in earlier documentation did not retain exact aggregate/component versions, complete hardware details, failure rates and locations of raw reports together. They are therefore no longer retained as public baselines. This document does not publish startup or restore latency, cache hit rates, node capacity or storage savings without an evidence chain.

## 1. Measurement entry points

Project-level aggregate entry points:

```bash
make perf E2E_WORKDIR="$PREPARED"
make perf-sandbox E2E_WORKDIR="$PREPARED"
make perf-sandbox-manifest
make perf-sandbox-working-set
make perf-density
make test-uffd-performance-gate
make test-perf-tools
```

`make perf` first builds the current sibling-repository sources, then runs accelerator's cache benchmark and the project's sandbox, Manifest, working-set and density harnesses. To run the cache benchmark separately:

```bash
make -C ../accelerator perf-cache
```

Real MicroVM paths require read/write access to `/dev/kvm`, root or noninteractive sudo, and the necessary images and runtime artifacts. Manifest and density harnesses additionally check Docker, networking and filesystem tools as specified by their scripts. A skip caused by missing prerequisites is not successful performance validation. Release evidence must record actual exit status and every failed sample.

## 2. Result evidence contract

Any result intended for documentation, release notes or capacity planning must retain at least:

| Dimension | Required evidence |
| --- | --- |
| Version | Aggregate tag/commit and exact tags/commits for accelerator, connector, sandboxer, orchestrator, runtime and vmlinux |
| Host | CPU model and topology, memory, disk, network, Linux, KVM/VMM, and bare-metal or nested-virtualization environment |
| Data path | Local file, named shared file or Manifest; FS/S3-compatible backend; cache levels and cold/hot state |
| Sandbox | vCPU, memory zone, declared resources, disks, kernel/runtime and template or snapshot source |
| Workload | Image digest, startup/restore steps, input data, concurrency, duration and random seed |
| Statistics | Total samples, successful/failed/skipped counts, failure rate, aggregation method and reported percentiles |
| Timing | Start/end events, whether download/build/import is included, clock source and timeout |
| Source | Harness command, environment variables, raw logs, machine-readable samples and CI run URL |

`sandbox-perf.sh` runs current `sandbox.lifecycle.sh` and `image.manifest-boot.sh`
through the public runner; `PREPARED` must already contain those prepared cases.
The source-only harness is not shipped in platform archives. Make calls its
source path directly without rebuilding prepared products. Case wall time includes
setup, assertions and cleanup: it is neither `Sandbox.create()`-to-ready time nor
directly comparable to historical T0-to-exit measurements. Raw stats, runner
results and failure logs are retained; mutable case state uses a separate short
`$TMPDIR/kp-*` directory (`/var/tmp` by default, `/build/t` inside workbench)
to keep Unix socket paths bounded, with its location recorded beside the report; the first failure exits nonzero. Other
`make perf` harnesses still use their source-build inputs; record each identity
rather than assuming it matches `PREPARED`. `sandbox-perf-manifest.sh` still
aggregates successful samples only, so retain requested iterations, full logs and
successful/failed/skipped counts rather than relying on its successful `N` alone.

A result missing a necessary dimension can support local diagnosis, but cannot be presented as a project-wide performance fact. Design thresholds must be labeled as a target or regression gate. Passing a gate only establishes that the candidate meets that test contract; it does not automatically establish a production SLO.

## 3. Cache and data paths

### 3.1 Measurement subjects

The accelerator cache benchmark observes local cache, tiered cache, shard fan-out and store origin separately. Distinguish at least:

- value size, prefill count, concurrency and run duration;
- client/server CPU affinity and network transport;
- L1/L2 cold, warm and partial-hit states;
- FS or S3-compatible origin;
- Get/Put p50, p95, p99, throughput, errors and origin-access fraction.

`CACHE_CTL_TIMING=1` supplies internal cache-stage diagnostics. Use pprof and tracing to locate CPU, allocation, network and scheduling costs. Internal timings do not replace the client's end-to-end measurement.

### 3.2 Interpreting paths

Compare these three artifact paths separately:

| Path | Cross-node requirement | Cache behavior | Restore dependency |
| --- | --- | --- | --- |
| Local file | Node affinity or operator-managed copying | Filesystem page cache | Original node files must remain available |
| Named shared file | NAS/NFS/shared filesystem visible to the target node | Shared-filesystem and host page caches | Conversion to Manifest is not required |
| Manifest | FS/S3-compatible store reachable from the target node | Direct origin access or local/tiered cache | Content located through the complete Manifest parent chain |

Do not interpret every ordinary `file://` reference as incapable of sharing. A named location on a shared filesystem can be accessible across nodes. Nor is a Manifest cold-cache measurement a fixed cost for every subsequent request.

Content-reuse results describe observations for the particular dataset, security domain and parent relationships. Instances of a common template primarily share explicit read-only parents. Coincidentally identical memory bytes in different running VMs are not a prerequisite for snapshot efficiency.

## 4. Sandbox startup, snapshots and restoration

### 4.1 Basic matrix

`test/perf/sandbox-perf.sh` characterizes current file/Manifest lifecycle cases with the timing scope above. `test/perf/sandbox-perf-manifest.sh` expands the matrix to Manifest cold/hot cache, snapshot publication and restoration. Reports should include at least:

- wall-clock time from the start request to guest-application readiness;
- sandboxer internal stages, VMM and guest-readiness evidence;
- on-demand read requests, bytes, errors and cache sources;
- separate durations for snapshot publication, parent resolution and restoration;
- requested iterations, successful samples, failed/skipped counts and failure rate, plus percentiles of successful samples.

`<run-root>/<sid>/ctl.sock` proves only that the host control socket exists. A performance harness must use an actual guest command, health check or workload-readiness condition as its completion signal.

### 4.2 Working-set matrix

`test/perf/sandbox-perf-working-set.sh` performs paired capture, publication, cold restore and optional prefetch comparisons against the same immutable parent. It produces:

```text
environment.json
samples.jsonl
local-crypto-samples.jsonl
report.md
raw/
```

`environment.json` records revisions, host, image, binaries and workload. `samples.jsonl` retains individual sample facts. `raw/` retains snapshot, publisher, restore and cache evidence. External citations must preserve the complete directory and provide the corresponding CI run URL, rather than extracting one percentile from `report.md`.

The mainline CI working-set step is a smoke test for obvious regressions. A formal performance report must explicitly set sample counts, retain all samples and label smoke tests separately from statistical reports.

### 4.3 Snapshot semantics

Measurements should separately cover:

- 1:N creation of independent instances from a shared template parent;
- 1:1 pause and resume of the same stable Sandbox ID;
- local restoration, named shared-file restoration and remote Manifest restoration;
- successful memory/disk parent-chain resolution, and failures from missing parents or integrity checks;
- cold cache, warm cache and explicit prefetch.

Snapshot layering, data carrier and cache state are three independent variables. A result for one cannot substitute for the other two.

## 5. Node resources and density

### 5.1 Workload model

`test/perf/workload.py` provides three workload classes: `idle`, deterministic grow/rest cycles and heavy-tailed active/idle activity. When running `test/perf/density-perf.sh`, explicitly record concurrency, memory zone, resource floor/startup/capacity, workload parameters, observation window and random seed.

Reports observe all of the following:

- admission, settled, grant, reject and terminal state for every sandbox;
- host `MemAvailable` and sandbox cgroup `memory.current/events.local`;
- guest workload completion and both guest/cgroup OOM;
- timelines for startup, activity, reclamation and termination;
- Reservation-pool watermarks, startup reserve and operational margin.

### 5.2 Interpretation boundaries

Density depends on workload peak working set, active fraction, resident VMM/guest overhead, reclamation latency, pause policy and node safety margin. The memory zone is a limit, not a constant that can be directly converted into physical occupancy or instance count.

Capacity planning must include guest kernel, page tables, slab and guest-service memory in addition to application RSS, with headroom measured for the workload. An OOM inside the guest is distinct from a host cgroup OOM; zero host `memory.events` OOM counters do not prove that the guest application survived. Record application completion, exits and guest evidence as well. Exit 137 identifies a SIGKILL-style exit, not by itself its cause. No universal guest-overhead number or fixed capacity multiplier is established by this document.

Explain resource responsibilities separately:

- sandboxer coordinates Balloon, Cgroup, VMM and pause/resume for an individual sandbox;
- the node-ctl Reservation Controller manages admission, the shared pool, watermarks, Grant, Inventory and recovery, without taking over the individual sandbox loop.

Balloon is not the only source of elasticity. Idle-CPU scheduling, inactive-memory reclaim, Cgroup limits, node admission and pausing long waits all affect effective utilization. Every capacity conclusion must report OOM and loss of useful work within the declared-resource and admission model. OOM must not be counted as successful overcommit.

High density is a result of improved resource utilization, not a promise of a predetermined instance count.

### 5.3 Agent business-completion benchmark

The existing `density-perf.sh` validates resource-control behavior; it is not a complete Agent business-throughput benchmark. `test/perf/agent-density/benchmark.py` checks the prepared BMS coding-Agent fixture's `summary.json` and `process-exit.json`. It requires 24 or 48 logical Agents, sequential fixed-three versus dynamic-sixteen wave plans (with a smaller final wave when needed), complete admission and business completion, correct work counts and deterministic output hashes, no host OOM kills, successful cleanup and clean process exits. Tasks restart their **IDs**, not their work, at zero in each wave: comparisons flatten declared wave order and then wave-local task ID. Rejected admission is **not** business completion. Sampled running peaks are reported separately and do not establish an allocator's configured limit.

The checker requires a disabled business-retry plan and one observed business start with matching task/start identity. It reports `planned_business_retries` and `observed_business_restarts`; it does **not** independently audit all client POST attempts. Non-200 HTTP polls and transport failures are counted separately, including late replies, and cross-checked against per-request records and per-task counters. A failed poll is not automatically a failed or retried business task. An HTTP 200 response with failed identity validation, an incorrect final result, or incomplete work still fails validation. Preserve per-wave `timeline.jsonl`, raw request/process logs and exact environment/versions for independent audit. The checker does not launch sandboxes or certify those external records. Its `seconds` and `speedup` use the sum of per-wave warmup and execution; outer case wall time also includes setup/cleanup.

On 2026-10-08 the local **matched** BMS study used 8C16G, a 12GiB node pool, 1C4G sandbox capacity, 256MiB allocatable, 512MiB startup and no swap. The synthetic coding-Agent workload used the `held` profile, a 192MiB peak parameter and four tool-work rounds per Agent. The matched 48-Agent runs completed 48/48: x86_64 took **851s fixed versus 380s dynamic**, and ARM64 **802s versus 333s**, measured from the outer case START/DONE records including setup and cleanup. The corresponding checker warmup-plus-execution times were 832.21s/350.49s and 784.44s/302.53s. Plans disabled business retries, observed start counts were one, and host OOM/cleanup checks passed. The x86 dynamic run recorded **734 successful polls out of 735**, including one HTTP error; its business work nevertheless completed correctly. Do not describe this as zero forwarding failures. These are workload- and version-specific observations, not a comparison with a different static memory manager or a production capacity guarantee. See `test/perf/agent-density/README.md` for the 24/48-Agent evidence table and audit boundaries.

## 6. Cluster control plane

Cluster performance must distinguish hot and cold paths:

```text
hot path:
client ──► router READY route cache ──► node/proxy/envd

cold path:
router ──► registry route_link ──► node/placer
node   ──► registry node_link/node_list
placer ──► group import and placement
```

Hot-path validation checks that a route-cache hit does not enter Resolve/Reserve. Measure create, connect, exec-session, data activation, node-state changes, group import and membership changes separately on cold paths. Every result must record registry replica/owner configuration, sandbox-group count, route-cache state, node count, request-completion conditions and failure rate.

Placer only imports groups and recommends placement. Node admission performs final resource confirmation. Placer throughput must not be described as the throughput of completed MicroVM creation.

## 7. Release and CI evidence

Source-candidate CI builds exact product and helper revisions before shared preparation. Product E2E runs flat cases through `test/e2e/e2e`; source/unit/race/vet and UFFD performance remain separate, and working-set smoke has an independent required job and result. Exact-assets validation consumes the packaged products, helpers and runner without rebuilding them. Architecture results record each selected case, explicit exclusions and the prepared input identity.

Workflow and evidence entry points:

- [`.github/workflows/integration-tests.yml`](../.github/workflows/integration-tests.yml): source-candidate Integration E2E and release asset validation;
- [`test/perf/`](../test/perf/): project performance harnesses and report generators;
- [`test/e2e/e2e`](../test/e2e/e2e): shared prepared-product case runner.

A green aggregate status alone is not evidence for a performance claim. Cite the run URL, base/head SHA, mode, relevant job log and downloaded raw artifact. If a workflow ran only smoke tests, identify it as `smoke`; do not relabel it as complete statistical validation.

## 8. Regression checks

For cache-path changes:

```bash
GOWORK=off make -C ../accelerator test
make -C ../accelerator perf-cache
```

For sandbox snapshot/restore/on-demand-read changes:

```bash
GOWORK=off make -C ../sandboxer test
make perf-sandbox E2E_WORKDIR="$PREPARED"
make perf-sandbox-manifest
make perf-sandbox-working-set
make test-uffd-performance-gate
```

For node-resource-control changes:

```bash
GOWORK=off make -C ../orchestrator test
make perf-density
```

For report-script or gate-parser changes:

```bash
make test-perf-tools
```

Use identical harness parameters and environments for each comparison, and inspect successful samples, failures/skips, raw logs and machine-readable output together. A candidate exceeding an approved gate must first have its root cause identified. Do not update a public baseline without complete evidence.

## 9. See also

- [`kuasar-sandbox.md`](kuasar-sandbox.md) - system semantics, component boundaries and performance-evidence requirements
- [`deployment.md`](deployment.md) - data backends, process topology and deployment choices
- [accelerator Cache](https://github.com/kuasar-sandbox/accelerator/blob/main/docs/cache.md) (archive: `docs/cache.md`) - cache architecture and component benchmark
- [sandboxer lifecycle](https://github.com/kuasar-sandbox/sandboxer/blob/main/docs/sandbox.md) (archive: `docs/sandbox.md`) - snapshot/restore and statistics fields
- [node resources](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-resource.md) (archive: `docs/node-resource.md`) - node resource-control protocol
- [cluster design](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/cluster.md) (archive: `docs/cluster.md`) - registry/router/placer design
