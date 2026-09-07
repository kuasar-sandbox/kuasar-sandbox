# perf — Performance validation and tuning

This document defines measurement boundaries, entry points, evidence requirements,
and regression methods for Kuasar Sandbox. A result applies only to its recorded
versions, hardware, data path, cache state, sandbox configuration, and workload;
a development-environment measurement cannot be extrapolated to every production
deployment.

Earlier absolute measurements did not retain exact aggregate/component revisions,
complete hardware details, failure rates, and raw-report locations together, so
they are no longer maintained as public baselines. This document does not publish
startup/restore latency, cache hit rate, node capacity, or storage-benefit numbers
without an evidence chain.

## 1. Measurement entry points

The project repository provides these aggregate entry points:

```bash
make perf
make perf-sandbox
make perf-sandbox-manifest
make perf-sandbox-working-set
make perf-density
make test-uffd-performance-gate
make test-perf-tools
```

`make perf` builds the current sibling-repository sources, then runs accelerator's
cache benchmark and the project sandbox, Manifest, working-set, and density
harnesses. Run the cache benchmark independently with:

```bash
make -C ../accelerator perf-cache
```

Real MicroVM paths require readable/writable `/dev/kvm`, root or noninteractive
sudo, and the necessary images and runtime artifacts. Manifest and density
harnesses also check Docker, networking, and filesystem tools. A skip caused by
missing prerequisites is not successful performance validation. Release evidence
must record the actual exit status and all failed samples.

## 2. Results evidence contract

Any result intended for documentation, release notes, or capacity planning must
retain at least the following evidence together:

| Dimension | Required evidence |
| --- | --- |
| Version | Aggregate tag/commit plus exact tags/commits for accelerator, connector, sandboxer, orchestrator, runtime, and vmlinux |
| Host | CPU model/topology, memory, disks, network, Linux, KVM/VMM, and bare-metal versus nested virtualization |
| Data path | Local file, named shared file, or Manifest; FS/S3-compatible backend; cache tiers and cold/hot state |
| Sandbox | vCPU, memory zone, resource declarations, disks, kernel/runtime, and template or snapshot source |
| Workload | Image digest, startup/restore steps, input data, concurrency, duration, and random seed |
| Statistics | Total samples, success/failure/skip counts, failure rate, aggregation method, and reported percentiles |
| Timing | Start/end events, whether download/build/import is included, clock source, and timeout |
| Source | Harness command, environment variables, raw logs, machine-readable samples, and CI run URL |

The current aggregate output from `sandbox-perf.sh` and
`sandbox-perf-manifest.sh` retains successful iterations and reports their count
`N`; failed iterations appear only in the full run log. Preserve the requested
`ITERS`, full log, and success/failure/skip counts when using these entry points.
An aggregate result without a recoverable total denominator and failure rate is
not sufficient evidence on its own.

A result missing a required dimension may aid local diagnosis, but is not a
project-level performance fact. Label design thresholds as targets or regression
gates. Passing a gate only establishes that the candidate satisfies that test
contract; it does not automatically establish a production SLO.

## 3. Cache and data paths

### 3.1 Measurement subjects

The accelerator cache benchmark observes local cache, tiered cache, shard fan-out,
and store origin separately. Distinguish at least:

- value size, prefill count, concurrency, and duration;
- client/server CPU affinity and network transport;
- cold, warm, and partially populated L1/L2 states;
- FS versus S3-compatible origin;
- Get/Put p50, p95, p99, throughput, errors, and origin-access ratio.

`CACHE_CTL_TIMING=1` provides internal phase diagnostics. Use pprof and tracing to
locate CPU, allocation, network, and scheduling overhead. Internal timings do not
replace client-observed end-to-end measurement.

### 3.2 Interpreting paths

Compare the three artifact paths separately:

| Path | Cross-node prerequisite | Cache conditions | Restore dependency |
| --- | --- | --- | --- |
| Local file | Node affinity or operator-managed copying | Filesystem page cache | Original node-local files must still exist |
| Named shared file | NAS/NFS/shared filesystem visible on the target node | Shared-filesystem and host page caches | No conversion to Manifest is required |
| Manifest | FS/S3-compatible store reachable from the target node | Direct origin access or local/tiered cache | Complete Manifest parent chain locates content |

Do not interpret every `file://` reference as non-shareable. A named location on
a shared filesystem can be accessed across nodes. Likewise, one cold-cache
Manifest cost is not a fixed cost for all subsequent requests.

Content-reuse measurements describe the observed dataset, security domain, and
parent relationships. Instances from one template primarily share an explicit
read-only parent. Identical bytes that happen to arise in independently running
VMs are not a prerequisite for snapshot efficiency.

## 4. Sandbox startup, snapshots, and restore

### 4.1 Basic matrix

`test/perf/sandbox-perf.sh` compares file and Manifest cold-start paths.
`test/perf/sandbox-perf-manifest.sh` expands Manifest cold/hot cache,
snapshot-publication, and restore scenarios. Reports should include at least:

- wall-clock time from the startup request to guest application readiness;
- sandboxer internal phases, VMM, and guest-readiness evidence;
- on-demand requests, bytes, errors, and cache sources;
- separate durations for snapshot publication, parent resolution, and restore;
- requested iterations, successful samples, failure/skip counts and failure rate,
  and percentiles of successful samples.

`<run-root>/<sid>/ctl.sock` only proves that the host control socket exists. The
harness must use a real guest command, health check, or workload-ready condition
as its completion signal.

### 4.2 Working-set matrix

`test/perf/sandbox-perf-working-set.sh` performs paired capture, publication,
cold-restore, and optional prefetch comparisons against the same immutable
parent. It produces:

```text
environment.json
samples.jsonl
local-crypto-samples.jsonl
report.md
raw/
```

`environment.json` records revisions, host, image, binaries, and workload.
`samples.jsonl` retains per-sample facts. `raw/` contains snapshot, publisher,
restore, and cache evidence. External citations must preserve the complete
output directory and its CI run URL, rather than extracting one percentile from
`report.md`.

The mainline BMS working-set job is a smoke test for obvious regressions. A formal
performance report must explicitly set its sample count, retain every sample,
and distinguish a smoke test from a statistical report in its naming.

### 4.3 Snapshot semantics

Measure these cases separately:

- 1:N creation of independently identified instances from one template parent;
- 1:1 pause/resume of the same stable Sandbox ID;
- local restore, named shared-file restore, and remote Manifest restore;
- success with complete memory/disk parent chains, and errors for missing parents
  or failed verification;
- cold cache, warm cache, and explicit prefetch.

Layering, data carrier, and cache state are three independent variables. A result
for one must not stand in for the others.

## 5. Node resources and density

### 5.1 Workload model

`test/perf/workload.py` provides `idle`, deterministic grow/rest cycles, and
heavy-tailed active/idle workloads. When running `test/perf/density-perf.sh`,
explicitly record concurrency, memory zone, resource floor/startup/capacity,
workload parameters, observation window, and random seed.

Observe all of the following together:

- per-sandbox admission, settled, grant, reject, and terminal state;
- host `MemAvailable` and sandbox cgroup `memory.current/events.local`;
- whether guest workloads complete, and guest/cgroup OOM events;
- startup, active, reclaim, and termination timelines;
- Reservation-pool watermarks, startup reserve, and operational margin.

### 5.2 Interpretation boundaries

Density depends jointly on peak working set, active fraction, persistent
VMM/guest overhead, reclaim latency, pause policy, and node safety margin.
A memory zone is a limit, not a constant convertible directly to physical charge
or instance count.

Keep resource responsibilities distinct:

- sandboxer executes per-sandbox balloon, cgroup, VMM, and pause/resume coordination;
- the node-ctl Reservation Controller executes admission, shared-pool and
  watermark policy, Grant, Inventory, and recovery. It does not take over the
  per-sandbox control loop.

Ballooning is not the only source of elasticity. Idle CPU scheduling, reclaim of
inactive memory, cgroup limits, node admission, and pausing long-waiting instances
all affect utilization. Capacity conclusions must report OOM and loss of useful
work within the declared-resource/admission model. OOM is not successful
oversubscription.

High density is a result of better utilization, not a promised instance count
chosen in advance.

## 6. Cluster control plane

Distinguish hot and cold cluster paths:

```text
hot path:
client ──► router READY route cache ──► node/proxy/envd

cold path:
router ──► registry route_link ──► node/placer
node   ──► registry node_link/node_list
placer ──► group import and placement
```

For the hot path, verify that a route-cache hit does not enter Resolve/Reserve.
For cold paths, measure create, connect, exec-session, data activation, node-state
changes, group import, and membership changes separately. Each result must record
registry replica/owner configuration, sandbox-group count, route-cache state,
node count, request-completion condition, and failure rate.

The placer imports groups and proposes placement. Node admission confirms the
resources. Placer throughput is not the throughput of completed MicroVM creation.

## 7. Release and CI evidence

Source-candidate BMS builds exact revisions, runs owner E2E, the UFFD regression
gate, and working-set smoke, and uploads a `ci-metadata-<run-id>-<attempt>` artifact.
Aggregate-release exact-assets mode extracts all assets from the same aggregate
bundle and runs the complete `test/e2e/run_all.sh` on real KVM.

Workflow and evidence entry points:

- [`.github/workflows/bms-e2e.yml`](../.github/workflows/bms-e2e.yml) — source-candidate
  and exact-assets BMS.
- [`test/perf/`](../test/perf/) — project performance harnesses and report generators.
- [`test/e2e/run_all.sh`](../test/e2e/run_all.sh) — prebuilt aggregate owner/platform E2E.

A green aggregate status alone is not evidence of a particular performance
conclusion. Cite the run URL, base/head SHA, execution mode, relevant job log,
and downloaded raw artifact. When the workflow only ran smoke, call it `smoke`,
not complete statistical validation.

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

For node resource-control changes:

```bash
GOWORK=off make -C ../orchestrator test
make perf-density
```

For report-script or gate-parser changes:

```bash
make test-perf-tools
```

Use the same harness parameters and environment for each comparison, and inspect
successful samples, failures/skips, raw logs, and machine-readable outputs
together. Investigate the cause before accepting a candidate beyond an approved
gate. Do not update a public baseline without complete evidence.

## 9. See also

- [`kuasar-sandbox.md`](kuasar-sandbox.md) — system semantics, component boundaries,
  and performance evidence requirements.
- [`deployment.md`](deployment.md) — data backends, process topology, and deployment choices.
- [Cache specification](https://github.com/kuasar-sandbox/accelerator/blob/main/docs/cache.md)
  (package: `docs/cache.md`) — cache architecture and component benchmarks.
- [Sandbox specification](https://github.com/kuasar-sandbox/sandboxer/blob/main/docs/sandbox.md)
  (package: `docs/sandbox.md`) — snapshot/restore and statistics fields.
- [Node resources](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-resource.md)
  (package: `docs/node-resource.md`) — node resource-control protocol.
- [Cluster specification](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/cluster.md)
  (package: `docs/cluster.md`) — registry/router/placer design.
