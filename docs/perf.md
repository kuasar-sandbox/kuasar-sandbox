[English](perf.md) | [简体中文](perf_zh.md)

<a id="perf---性能验证与调优"></a>
# perf - Performance validation and tuning

This document defines measurement boundaries, entry points, evidence requirements and regression methods for Kuasar Sandbox performance testing. Results apply only to the recorded versions, hardware, data path, cache state, sandbox specification and workload. A development-environment measurement cannot be extrapolated to all production deployments.

Historical absolute numbers in earlier documentation did not retain exact aggregate/component versions, complete hardware details, failure rates and locations of raw reports together. They are therefore no longer retained as public baselines. This document does not publish startup or restore latency, cache hit rates, node capacity or storage savings without an evidence chain.

<a id="1-测量入口"></a>
## 1. Measurement entry points

Project-level aggregate entry points:

```bash
make perf
make perf-sandbox
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

<a id="2-结果证据合同"></a>
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

The aggregate outputs of the current `sandbox-perf.sh` and `sandbox-perf-manifest.sh` retain only successful iterations and report the successful sample count `N`. Failed iterations appear only in the complete run log. When using these entry points, also retain the requested `ITERS`, complete logs and successful/failed/skipped counts. Aggregate output that cannot recover the total denominator and failure rate is insufficient on its own as performance evidence.

A result missing a necessary dimension can support local diagnosis, but cannot be presented as a project-wide performance fact. Design thresholds must be labeled as a target or regression gate. Passing a gate only establishes that the candidate meets that test contract; it does not automatically establish a production SLO.

<a id="3-cache-与数据路径"></a>
## 3. Cache and data paths

<a id="31-测量对象"></a>
### 3.1 Measurement subjects

The accelerator cache benchmark observes local cache, tiered cache, shard fan-out and store origin separately. Distinguish at least:

- value size, prefill count, concurrency and run duration;
- client/server CPU affinity and network transport;
- L1/L2 cold, warm and partial-hit states;
- FS or S3-compatible origin;
- Get/Put p50, p95, p99, throughput, errors and origin-access fraction.

`CACHE_CTL_TIMING=1` supplies internal cache-stage diagnostics. Use pprof and tracing to locate CPU, allocation, network and scheduling costs. Internal timings do not replace the client's end-to-end measurement.

<a id="32-路径解释"></a>
### 3.2 Interpreting paths

Compare these three artifact paths separately:

| Path | Cross-node requirement | Cache behavior | Restore dependency |
| --- | --- | --- | --- |
| Local file | Node affinity or operator-managed copying | Filesystem page cache | Original node files must remain available |
| Named shared file | NAS/NFS/shared filesystem visible to the target node | Shared-filesystem and host page caches | Conversion to Manifest is not required |
| Manifest | FS/S3-compatible store reachable from the target node | Direct origin access or local/tiered cache | Content located through the complete Manifest parent chain |

Do not interpret every ordinary `file://` reference as incapable of sharing. A named location on a shared filesystem can be accessible across nodes. Nor is a Manifest cold-cache measurement a fixed cost for every subsequent request.

Content-reuse results describe observations for the particular dataset, security domain and parent relationships. Instances of a common template primarily share explicit read-only parents. Coincidentally identical memory bytes in different running VMs are not a prerequisite for snapshot efficiency.

<a id="4-sandbox-启动快照与恢复"></a>
## 4. Sandbox startup, snapshots and restoration

<a id="41-基础矩阵"></a>
### 4.1 Basic matrix

`test/perf/sandbox-perf.sh` compares file and Manifest cold-start paths. `test/perf/sandbox-perf-manifest.sh` expands the matrix to Manifest cold/hot cache, snapshot publication and restoration. Reports should include at least:

- wall-clock time from the start request to guest-application readiness;
- sandboxer internal stages, VMM and guest-readiness evidence;
- on-demand read requests, bytes, errors and cache sources;
- separate durations for snapshot publication, parent resolution and restoration;
- requested iterations, successful samples, failed/skipped counts and failure rate, plus percentiles of successful samples.

`<run-root>/<sid>/ctl.sock` proves only that the host control socket exists. A performance harness must use an actual guest command, health check or workload-readiness condition as its completion signal.

<a id="42-working-set-矩阵"></a>
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

<a id="43-快照语义"></a>
### 4.3 Snapshot semantics

Measurements should separately cover:

- 1:N creation of independent instances from a shared template parent;
- 1:1 pause and resume of the same stable Sandbox ID;
- local restoration, named shared-file restoration and remote Manifest restoration;
- successful memory/disk parent-chain resolution, and failures from missing parents or integrity checks;
- cold cache, warm cache and explicit prefetch.

Snapshot layering, data carrier and cache state are three independent variables. A result for one cannot substitute for the other two.

<a id="5-节点资源与密度"></a>
## 5. Node resources and density

<a id="51-workload-模型"></a>
### 5.1 Workload model

`test/perf/workload.py` provides three workload classes: `idle`, deterministic grow/rest cycles and heavy-tailed active/idle activity. When running `test/perf/density-perf.sh`, explicitly record concurrency, memory zone, resource floor/startup/capacity, workload parameters, observation window and random seed.

Reports observe all of the following:

- admission, settled, grant, reject and terminal state for every sandbox;
- host `MemAvailable` and sandbox cgroup `memory.current/events.local`;
- guest workload completion and both guest/cgroup OOM;
- timelines for startup, activity, reclamation and termination;
- Reservation-pool watermarks, startup reserve and operational margin.

<a id="52-解释边界"></a>
### 5.2 Interpretation boundaries

Density depends on workload peak working set, active fraction, resident VMM/guest overhead, reclamation latency, pause policy and node safety margin. The memory zone is a limit, not a constant that can be directly converted into physical occupancy or instance count.

Capacity planning must include guest kernel, page tables, slab and guest-service memory in addition to application RSS, with headroom measured for the workload. An OOM inside the guest is distinct from a host cgroup OOM; zero host `memory.events` OOM counters do not prove that the guest application survived. Record application completion, exits and guest evidence as well. Exit 137 identifies a SIGKILL-style exit, not by itself its cause. No universal guest-overhead number or fixed capacity multiplier is established by this document.

Explain resource responsibilities separately:

- sandboxer coordinates Balloon, Cgroup, VMM and pause/resume for an individual sandbox;
- the node-ctl Reservation Controller manages admission, the shared pool, watermarks, Grant, Inventory and recovery, without taking over the individual sandbox loop.

Balloon is not the only source of elasticity. Idle-CPU scheduling, inactive-memory reclaim, Cgroup limits, node admission and pausing long waits all affect effective utilization. Every capacity conclusion must report OOM and loss of useful work within the declared-resource and admission model. OOM must not be counted as successful overcommit.

High density is a result of improved resource utilization, not a promise of a predetermined instance count.

<a id="6-cluster-控制面"></a>
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

<a id="7-release-与-ci-证据"></a>
## 7. Release and CI evidence

Source-candidate Integration E2E builds exact revisions, runs owner E2E, the UFFD regression gate and working-set smoke, and uploads a `ci-metadata-<run-id>-<attempt>` artifact. Aggregate-release exact-assets mode extracts all assets from the same aggregate package and runs the complete `test/e2e/run_all.sh` on real KVM.

Workflow and evidence entry points:

- [`.github/workflows/integration-tests.yml`](../.github/workflows/integration-tests.yml): source-candidate Integration E2E and release asset validation;
- [`test/perf/`](../test/perf/): project performance harnesses and report generators;
- [`test/e2e/run_all.sh`](../test/e2e/run_all.sh): aggregate prebuilt owner and platform E2E.

A green aggregate status alone is not evidence for a performance claim. Cite the run URL, base/head SHA, mode, relevant job log and downloaded raw artifact. If a workflow ran only smoke tests, identify it as `smoke`; do not relabel it as complete statistical validation.

<a id="8-回归检查"></a>
## 8. Regression checks

For cache-path changes:

```bash
GOWORK=off make -C ../accelerator test
make -C ../accelerator perf-cache
```

For sandbox snapshot/restore/on-demand-read changes:

```bash
GOWORK=off make -C ../sandboxer test
make perf-sandbox
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
