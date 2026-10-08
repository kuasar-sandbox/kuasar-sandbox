# Agent density: business-completion evidence

This tool **validates evidence** from the matched fixed-three versus dynamic-sixteen
coding-Agent experiment. It does not create MicroVMs, retry rejected tasks, or
fabricate a static memory allocator. The launcher is currently a prepared BMS
orchestrator E2E fixture, **not** a portable checked-in launch command; no
`make perf` gate is claimed for it.

## Validation contract

Retain each run's `summary.json` (including its plan, task results,
`data_request_records` and `response_failures`), `process-exit.json`, per-wave
`timeline.jsonl`, environment/manifest, raw HTTP trace and process logs. Run:

```bash
python3 test/perf/agent-density/benchmark.py \
  --fixed /path/to/fixed --density /path/to/density --output report.json
make test-perf-tools
```

The checker requires 24 or 48 logical Agents, sequential waves of three versus
sixteen (allowing a smaller last wave), complete admission and correct business
completion, clean driver/observer exits, no host OOM kills and successful cleanup.
Task IDs reset to zero in each wave. Logical work is paired by **declared wave
order, then wave-local task ID**, not by a globally unique raw task ID or by result
arrival order. Duplicate or missing IDs within a wave are invalid. Per-Agent
profile, memory peak and work counts must match across runs. The fixture's
`rounds` actions, `rounds * 3` completed tools and SHA256 of `VALUE = <rounds>\n`
are checked, so two identical but incorrect outputs cannot pass.

A disabled `plan.business_retries` and one observed business start with matching
task/start identity are required. The report labels these as
`planned_business_retries` and `observed_business_restarts`; it does **not** audit
all raw client POST attempts. Review the retained HTTP trace and launcher when
making a stronger zero-client-retry claim. The checker does not certify external
version, environment or timeline records; retain and audit them separately.

Data-plane health is separate from business completion. Non-200 HTTP polls and
transport errors are reported as `http_errors` and `transport_errors`, with
`late_errors` identifying their subset returned after the fixture's polling loop.
Counters are reconciled with request records, failure diagnostics and per-task
in-loop counts. A failed poll does not by itself imply a restarted or failed
business task. HTTP 200 replies with failed identity validation, incorrect final
results, incomplete admission/work, host OOM or failed cleanup remain fatal.

The report's `seconds` and `speedup` use **sum of per-wave warmup + execution**.
Outer case wall time includes setup/cleanup and must be recorded separately.
Planned wave sizes define this experiment's concurrency; sampled `peak_running`
is only an observation, not proof of an allocator's configured limit. This is a
concurrency-controlled Kuasar comparison, **not** a comparison with Kubernetes
or a distinct static memory manager.

## Local matched observations, 2026-10-08

The BMS study used an 8-vCPU/16-GiB host container, a 12-GiB pool, 1-vCPU/4-GiB
sandbox capacity, 256-MiB allocatable, 512-MiB startup and no swap. The synthetic
coding-Agent workload used the `held` profile, a 192-MiB peak parameter and four
tool-work rounds per Agent. The four matched comparisons passed the checker:

| Architecture | Agents completed in each run | Outer fixed / dynamic (s) | Warmup + execution fixed / dynamic (s) | Successful / total polls, fixed; dynamic |
| --- | --- | --- | --- | --- |
| x86_64 | 24/24 | 432 / 200 | 416.04 / 178.86 | 360/360; 369/369 |
| x86_64 | 48/48 | 851 / 380 | 832.21 / 350.49 | 720/720; 734/735 |
| ARM64 | 24/24 | 409 / 193 | 391.96 / 170.99 | 336/336; 325/325 |
| ARM64 | 48/48 | 802 / 333 | 784.44 / 302.53 | 672/672; 643/643 |

Evidence identities are `agent-matched-{static,dynamic}{24,48}-20261008`.
Outer durations come from START/DONE timestamps in each architecture's
`agent-matched-20261008.log`, at one-second resolution; checker timings come from
`summary.json`. Do not substitute earlier queue experiments with non-equivalent
per-wave workload distributions for this matched series.

Plans disabled business retries and every Agent had one observed business start.
Host OOM and cleanup checks passed. Sampled simultaneous running peaks were
three versus sixteen; the dynamic recovery queue reached three. The x86 dynamic
48-Agent run had **one HTTP polling error** and still completed all business work
correctly. It must not be described as zero forwarding failures.

These are **local workload-specific observations**, not general capacity
promises or a reproducible release baseline. The raw BMS evidence remains
external to this repository. Retain the full evidence directory with exact
aggregate/component revisions, binary and image digests, host identity and
launcher source before using the observations in release or capacity decisions.
