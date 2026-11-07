[English](performance.md) | [简体中文](performance_zh.md)

# Performance measurement, capacity and tuning

This guide defines comparable measurements, capacity qualification and tuning. Historical results retain their environment and revision; they are not promises for every deployment.

This document defines measurement boundaries, entry points, evidence requirements and regression methods for Kuasar Sandbox performance testing. Results apply only to the recorded versions, hardware, data path, cache state, sandbox specification and workload. A development-environment measurement cannot be extrapolated to all production deployments.

Historical absolute numbers in earlier documentation did not retain exact aggregate/component versions, complete hardware details, failure rates and locations of raw reports together. They are therefore no longer retained as public baselines. This document does not publish startup or restore latency, cache hit rates, node capacity or storage savings without an evidence chain.

## Measurement entry points

Project-level aggregate entry points:

```bash
make perf E2E_WORKDIR="$PREPARED"
make perf-sandbox E2E_WORKDIR="$PREPARED"
make perf-sandbox-manifest
make perf-sandbox-working-set
make perf-density
make perf-warmpool-dedup
make test-uffd-performance-gate
make test-perf-tools
```

`make perf` first builds the current sibling-repository sources, then runs accelerator's cache benchmark and the project's sandbox, Manifest, working-set, density and warm-pool deduplication harnesses. The density harness has the compatibility limitations described under [Node resources and density](#node-resources-and-density); an aggregate target does not certify every harness against current products. To run the cache benchmark separately:

```bash
make -C ../accelerator perf-cache
```

Real MicroVM paths require read/write access to `/dev/kvm`, root or noninteractive sudo, and the necessary images and runtime artifacts. Manifest and density harnesses additionally check Docker, networking and filesystem tools as specified by their scripts. A skip caused by missing prerequisites is not successful performance validation. Release evidence must record actual exit status and every failed sample.

## Result evidence contract

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

## Cache and data paths

### Measurement subjects

The accelerator cache benchmark defaults to `BENCH_SCENARIO=local`. Select `tiered-l1` or `tiered-shard-l2` explicitly for tiered-cache or shard-fan-out measurements. Those managed scenarios use another local cache daemon as their upstream origin, not a real Store/FS/S3 backend. Store-origin performance requires a separately configured and recorded real topology. For comparable cache/data-path reports, distinguish at least:

- value size, prefill count, concurrency and run duration;
- client/server CPU affinity and network transport;
- L1/L2 cold, warm and partial-hit states;
- FS or S3-compatible origin;
- Get/Put p50, p95, p99, throughput, errors and origin-access fraction.

`CACHE_CTL_TIMING=1` supplies internal cache-stage diagnostics. Use pprof and tracing to locate CPU, allocation, network and scheduling costs. Internal timings do not replace the client's end-to-end measurement.

### Interpreting paths

Compare these three artifact paths separately:

| Path | Cross-node requirement | Cache behavior | Restore dependency |
| --- | --- | --- | --- |
| Local file | Node affinity or operator-managed copying | Filesystem page cache | Referenced files must be available at the restore target |
| Named shared file | NAS/NFS/shared filesystem visible to the target node | Shared-filesystem and host page caches | Conversion to Manifest is not required |
| Manifest | FS/S3-compatible store reachable from the target node | Direct origin access or local/tiered cache | The complete dependency graph, including retained file parents and their bindings, must be accessible at the target |

Do not interpret every ordinary `file://` reference as incapable of sharing. A named location on a shared filesystem can be accessible across nodes. Nor is a Manifest cold-cache measurement a fixed cost for every subsequent request.

`make perf-warmpool-dedup` compares unique nonzero chunk-hash overlap across independent boots of the same image, for overlay and memory snapshots; its single image ingest is a trivially shared sanity check. This controlled workload does not measure general cross-image deduplication.

With the Manifest configuration facade, a nonempty `cache.endpoint` selects the cache client; it does not also install a direct-store fallback on a cache miss or failure. Direct store reads are selected when `cache.endpoint` is empty. Record the cache server's configured origin behavior separately, rather than assuming that local-cache mode automatically fetches missing data.

Content-reuse results describe observations for the particular dataset, security domain and parent relationships. Instances of a common template primarily share explicit read-only parents. Coincidentally identical memory bytes in different running VMs are not a prerequisite for snapshot efficiency.

## Sandbox startup, snapshots and restoration

### Basic matrix

`test/perf/sandbox-perf.sh` characterizes current file/Manifest lifecycle cases with the timing scope above. `test/perf/sandbox-perf-manifest.sh` expands the matrix to Manifest cold/hot cache, snapshot publication and restoration. Reports should include at least:

- wall-clock time from the start request to guest-application readiness;
- sandboxer internal stages, VMM and guest-readiness evidence;
- on-demand read requests, bytes, errors and cache sources;
- separate durations for snapshot publication, parent resolution and restoration;
- requested iterations, successful samples, failed/skipped counts and failure rate, plus percentiles of successful samples.

`<run-root>/<sid>/ctl.sock` proves only that the host control socket exists. A performance harness must use an actual guest command, health check or workload-readiness condition as its completion signal.

### Working-set matrix

`test/perf/sandbox-perf-working-set.sh` performs paired capture, publication, cold restore and optional prefetch comparisons against the same immutable parent. It produces:

```text
environment.json
samples.jsonl
local-crypto-samples.jsonl
report.md
raw/
```

`environment.json` records revisions, host, image, binaries and workload. `samples.jsonl` retains individual sample facts. `raw/` retains snapshot, publisher, restore and cache evidence. External citations must preserve the complete directory and provide the corresponding CI run URL, rather than extracting one percentile from `report.md`.

The source-candidate CI plan selects `working-set-smoke` only for x86_64 when the selected owners include `platform` or `sandboxer`; it is not selected for every architecture or exact-assets run. The selected step sets `PERF_ITERS=1` to catch obvious regressions. A formal performance report must explicitly set sample counts, retain all samples and label smoke tests separately from statistical reports.

### Snapshot semantics

Measurements should separately cover:

- 1:N creation of independent instances from a shared template parent;
- 1:1 pause and resume of the same stable Sandbox ID;
- local restoration, named shared-file restoration and remote Manifest restoration;
- successful memory/disk parent-chain resolution, and failures from missing parents or integrity checks;
- cold cache, warm cache and explicit prefetch.

Snapshot layering, data carrier and cache state are three independent variables. A result for one cannot substitute for the other two.

## Node resources and density

### Workload model

`test/perf/workload.py` provides three workload classes: `idle`, deterministic grow/rest cycles and heavy-tailed active/idle activity. When running `test/perf/density-perf.sh`, explicitly record concurrency, memory zone, resource floor/startup/capacity, workload parameters, observation window and random seed.

The current density harness is not compatible as-is with the current conductor configuration: it still emits removed `proxy.mode`, `resource_listen.audit_path` and `resource_listen.dampening` fields. Its default `$WORK/run` also needs to satisfy the current maximum-ID Unix-socket path budget. Do not treat `make perf-density` as current-conductor qualification until the harness has been updated and validated against the exact component revisions.

The script's text report samples host `MemAvailable`, log-derived controller counters and cgroup `memory.events.local` (`high` and `oom`, not `oom_kill`). It does not establish guest business completion or collect the full per-pool and placement evidence below. Its printed `PASS` and projected ceiling are not a production capacity result; the final printed verdict is not itself propagated as a failing exit status. Retain stderr and work logs as well as the report (`PERF_KEEP=1` preserves the work directory).

A qualifying density report must observe all of the following:

- admission, settled, grant, reject and terminal state for every sandbox;
- host `MemAvailable` and sandbox cgroup `memory.current/events.local`;
- guest workload completion and both guest/cgroup OOM;
- timelines for startup, activity, reclamation and termination;
- each resource pool's scheduling capacity, runtime allocatable, watermarks, startup reserve, growth rate and recovery obligations;
- Runner/Builder pool mapping, idle `size`, slot `capacity`, `priority`, NUMA/CPU placement and actual masks. Compare pool-local pressure as well as aggregate node usage; unused capacity in another pool does not prove the selected pool can admit a request.

### Interpretation boundaries

Density depends on workload peak working set, active fraction, resident VMM/guest overhead, reclamation latency, pause policy and node safety margin. The memory zone is a limit, not a constant that can be directly converted into physical occupancy or instance count.

Capacity planning must include guest kernel, page tables, slab and guest-service memory in addition to application RSS, with headroom measured for the workload. An OOM inside the guest is distinct from a host cgroup OOM; zero host `memory.events` OOM counters do not prove that the guest application survived. Record application completion, exits and guest evidence as well. Exit 137 identifies a SIGKILL-style exit, not by itself its cause. No universal guest-overhead number or fixed capacity multiplier is established by this document.

Explain resource responsibilities separately:

- sandboxer coordinates Balloon, Cgroup, VMM and pause/resume for an individual sandbox;
- the node-ctl Reservation Controller manages admission, resource-pool budgets, watermarks, Grant, Inventory and recovery, without taking over the individual sandbox loop.

Balloon is not the only source of elasticity. Idle-CPU scheduling, inactive-memory reclaim, Cgroup limits, node admission and pausing long waits all affect effective utilization. Every capacity conclusion must report OOM and loss of useful work within the declared-resource and admission model. OOM must not be counted as successful overcommit.

High density is a result of improved resource utilization, not a promise of a predetermined instance count.

### Agent business-completion benchmark

The existing `density-perf.sh` is intended to characterize resource-control behavior, subject to the compatibility and evidence limits above; it is not a complete Agent business-throughput benchmark. `test/perf/agent-density/benchmark.py` checks the prepared BMS coding-Agent fixture's `summary.json` and `process-exit.json`. It requires 24 or 48 logical Agents, sequential fixed-three versus dynamic-sixteen wave plans (with a smaller final wave when needed), complete admission and business completion, correct work counts and deterministic output hashes, no host OOM kills, successful cleanup and clean process exits. Tasks restart their **IDs**, not their work, at zero in each wave: comparisons flatten declared wave order and then wave-local task ID. Rejected admission is **not** business completion. Sampled running peaks are reported separately and do not establish an allocator's configured limit.

The checker requires a disabled business-retry plan and one observed business start with matching task/start identity. It reports `planned_business_retries` and `observed_business_restarts`; it does **not** independently audit all client POST attempts. Non-200 HTTP polls and transport failures are counted separately, including late replies, and cross-checked against per-request records and per-task counters. A failed poll is not automatically a failed or retried business task. An HTTP 200 response with failed identity validation, an incorrect final result, or incomplete work still fails validation. Preserve per-wave `timeline.jsonl`, raw request/process logs and exact environment/versions for independent audit. The checker does not launch sandboxes or certify those external records. Its `seconds` and `speedup` use the sum of per-wave warmup and execution; outer case wall time also includes setup/cleanup.

On 2026-10-08 the local **matched** BMS study used 8C16G, a 12GiB node pool, 1C4G sandbox capacity, 256MiB allocatable, 512MiB startup and no swap. The synthetic coding-Agent workload used the `held` profile, a 192MiB peak parameter and four tool-work rounds per Agent. The matched 48-Agent runs completed 48/48: x86_64 took **851s fixed versus 380s dynamic**, and ARM64 **802s versus 333s**, measured from the outer case START/DONE records including setup and cleanup. The corresponding checker warmup-plus-execution times were 832.21s/350.49s and 784.44s/302.53s. Plans disabled business retries, observed start counts were one, and host OOM/cleanup checks passed. The x86 dynamic run recorded **734 successful polls out of 735**, including one HTTP error; its business work nevertheless completed correctly. Do not describe this as zero forwarding failures. These are workload- and version-specific observations, not a comparison with a different static memory manager or a production capacity guarantee. See `test/perf/agent-density/README.md` for the 24/48-Agent evidence table and audit boundaries.

## Cluster control plane

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

## Release and CI evidence

Source-candidate CI builds exact product and helper revisions before shared preparation. Product E2E runs flat cases through `test/e2e/e2e`; source/unit/race/vet and UFFD performance remain separate, and selected working-set smoke runs have an independent required job and result under the selection rule above. Exact-assets validation consumes the packaged products, helpers and runner without rebuilding them. Architecture results record each selected case, explicit exclusions and the prepared input identity.

Workflow and evidence entry points:

- [`.github/workflows/integration-tests.yml`](../.github/workflows/integration-tests.yml): source-candidate Integration E2E and release asset validation;
- [`test/perf/`](../test/perf): project performance harnesses and report generators;
- [`test/e2e/e2e`](../test/e2e/e2e): shared prepared-product case runner.

A green aggregate status alone is not evidence for a performance claim. Cite the run URL, base/head SHA, mode, relevant job log and downloaded raw artifact. If a workflow ran only smoke tests, identify it as `smoke`; do not relabel it as complete statistical validation.

## Regression checks

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

For node-resource-control changes, use the unit-test entry below. The density entry is listed for compatibility-qualified harness revisions only; see the limitations above:

```bash
GOWORK=off make -C ../orchestrator test
make perf-density
```

For report-script or gate-parser changes:

```bash
make test-perf-tools
```

Use identical harness parameters and environments for each comparison, and inspect successful samples, failures/skips, raw logs and machine-readable output together. A candidate exceeding an approved gate must first have its root cause identified. Do not update a public baseline without complete evidence.

<a id="capacity-example"></a>
## Capacity qualification with an assumed budget

This is an **arithmetic example, not measured performance**. Suppose a 16-vCPU,
32-GiB node runs an agent that executes Python, reads a repository, writes a small
workspace and makes external requests. Assume each active sandbox needs 1 GiB
normally, 2.5 GiB during synchronized work and 2 CPU cores at the acceptance load.
First measure those quantities with the actual image; OCI image size is not RSS.

An initial budget might reserve 4 GiB for the host, 4 GiB for fleet control/VMM
and non-workload overhead, 6 GiB for the whole accelerator/cache path, and 3 GiB
for one additional cold-recovery transient. That leaves
`32 - 4 - 4 - 6 - 3 = 15 GiB`, or six assumed 2.5-GiB burst workloads.
If only 12 CPU cores are budgeted for work, `12 / 2 = 6` independently gives the
same initial admission limit. Six normal workloads use an assumed 6 GiB, but
that does **not** justify admitting fifteen if all can become active together.
Guest capacity, allocatable/startup, controller grants and actual charged usage
are different quantities; apply the [Node resource contract](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-resource.md),
not this arithmetic as YAML defaults.

The 6-GiB cache allowance must include Go heap, native RocksDB block/blob cache
and memtables, inflight requests/blobs/decryption buffers, compaction and filesystem
page-cache pressure. `GOMEMLIMIT` or `disk_bytes * mem_ratio` alone is not a process
RSS limit. Record cgroup/RSS peaks, cache occupancy, compaction and disk/network
traffic using the [Cache budget contract](https://github.com/kuasar-sandbox/accelerator/blob/main/docs/cache.md).
Large reference YAML files demonstrate options and are not the recommended first
production size.

| Measurement | Run and record | Acceptance/adjustment |
|---|---|---|
| Normal load | Representative requests at 1, then 3, then 6 concurrent sandboxes; warm steady state | Application latency/error target, charged memory, CPU, storage growth and endpoint throughput meet your declared SLO. |
| Synchronized burst | Make all six exercise their peak phase together, including writes | No OOM/data loss; admission and controller headroom remain valid. If the assumed 2.5 GiB is exceeded, lower admission or increase budget before retesting. |
| Cold recovery | Restore the qualified workload from a cold test cache/destination while normal load continues | Measure restore-to-ready and first-use p95/p99, source attempts, bandwidth and total peak memory; include the recovery transient. Do not flush production caches. |
| Sustained run | Repeat pause/resume and cleanup long enough to include compaction and retention work | No unbounded RSS, disk, FD or inflight growth; all state assertions and cleanup pass. |

Set numerical application/restore SLOs before the run; there is no universal
latency promise here. Increase one budget/concurrency variable at a time only
after the complete matrix passes. Stop on integrity/identity failure or OOM,
retain the failure evidence, kill owned tests and return to the last qualified
limit. Report cold and warm measurements separately, including unavailable probes.

## See also

- [System architecture](architecture.md) - system semantics, component boundaries and performance-evidence requirements
- [`deployment.md`](deployment.md) - data backends, process topology and deployment choices
- [accelerator Cache](https://github.com/kuasar-sandbox/accelerator/blob/main/docs/cache.md) - cache architecture and component benchmark
- [sandboxer lifecycle](https://github.com/kuasar-sandbox/sandboxer/blob/main/docs/runtime.md) - snapshot/restore and statistics fields
- [node resources](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-resource.md) - node resource-control protocol
- [cluster design](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/cluster.md) - registry/router/placer design
