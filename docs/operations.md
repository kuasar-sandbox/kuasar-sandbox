[English](operations.md) | [简体中文](operations_zh.md)

# Operations, recovery and retention

This guide owns deployment health checks, diagnosis, recovery and retention. It assumes completed [deployment acceptance](deployment.md#production-start), retained version/effective configuration and known data owners. The [Orchestrator observability contract](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/observability.md) defines exact log and metric fields.

## Daily health and diagnosis order

1. Check the control API, Proxy data entry, node admission and the selected Store/Cache, shared files and gateway separately; a live process does not prove every path ready.
2. Record stable Sandbox ID, RunID, node, Group/route key for a cluster, operation and time window from the failed request. Correlate lifecycle state, logs, native usage and Telemetry with those identities.
3. Inspect control state before read-only statistics. Stats/metrics reads do not Wake a sandbox; use a separately authorized business operation when recovery itself must be exercised.
4. Validate execution with a command/file roundtrip and actual application response. A listener or socket file does not prove application readiness.
5. Retain the first failure and its versions/configuration before changing owned instances or services. Broad cleanup can erase the cause.

On the node, establish the current RunID and its pool's effective unit template before querying the runner. This example uses the default `sandbox-runner@.service`; for custom templates or multiple pools, form the actual unit name from that template and RunID:

```bash
RUN_ID="<current-run-id>"
journalctl --unit "sandbox-runner@$RUN_ID.service" --since "15 min ago" --no-pager
```

## Recovery and fault domains

- `sandbox-ctl`, the VMM, node, registry, router and placer are independently observable and recoverable processes/fault domains.
- Durable node records and runtime Inventory support restart recovery and orphan cleanup. A host socket's existence alone does not prove guest readiness; recovery must be confirmed through an actual health check or execution operation.
- Local artifacts support node-affine recovery; shared-file or Manifest artifacts make the data reachable from other nodes. Recovery still requires existing ownership, compatible inputs and target admission. Cluster recovery after the original node is declared gone also requires a caller-held valid MigrationToken and [restore eligibility](upgrading.md#restore-eligibility); readable snapshot data does not imply automatic migration.
- Registry stores cluster execution state; the node is authoritative for a running sandbox. Placer only recommends placement, and node admission still performs final resource confirmation.
- Group is the cluster partition key. Routing, placement, credential verification and operations remain group-scoped.

## Failure domains

| Failure | Direct impact | Recovery |
|---|---|---|
| Compute-node store-ctl crashes. | Local Manifest origin reads/writes stop; L1/L2 hits and native-file paths are unaffected. | systemd restart restores origin access. |
| Compute-node tiered cache crashes. | New sandbox faults requiring wire access wait/fail under client timeout/cancellation behavior. | systemd restarts it; persisted RocksDB data can restore L1 hits. Cache misses still fall through normally. |
| One shard fails under effective RS 4+1. | L2 can reconstruct from any four **distinct shard indices** among the selected five, if those shards are available. | Restart the shard. A peer outage does not automatically rewrite Maglev membership or create an extra parity peer. |
| At least two shards for one 4+1 key are unavailable. | Fewer than four distinct valid shards cause an L2 miss. | Read through origin, with added cost. Change membership one peer at a time and verify convergence. |
| Conductor crashes. | API/admission and new launches stop; existing sandboxes retain their last grants. | Restart and reconcile SQLite (`db_path`, default `<base_root>/node-ctl.db`) with live units from every effective runner template/pool (the default is `sandbox-runner@*.service`). Adopt active/running sandboxes and rearm TTL; mark running records without units dead; retain paused/snapshot records for connect/auto-resume. Resource inventory and reports reconstruct accounting; deprecated state_path is ignored. |
| S3-compatible origin unavailable. | Manifest operations needing that origin fail or wait according to their policies. | L1/L2 hits can continue. New origin writes, cold faults and final image upload are affected; independent local/shared-file paths are unaffected. |
| Entire compute node fails. | Running sandboxes on that node stop. | Isolate the node. Cluster recovery requires retained ownership, a caller-held valid MigrationToken and an eligible destination under the [complete restore checks](upgrading.md#restore-eligibility). Shared artifacts provide data reachability; local-only artifacts still depend on the original node/storage. Automatic recovery is not guaranteed. |
| L2 loses more than its parity allowance. | Affected keys miss in L2. | Tiered reads continue through origin where available; cache service resumes as peers/data recover. |

### Resource-pool recovery

Before changing pool names, unit templates or capacity, inspect the live pool and execution identities. Restart reconstructs execution slots and declared-capacity claims before admission; existing instances remain accounted even when newly lowered limits are exceeded. Keep live resource identities and unit templates configured until their executions are drained. Legacy live units shared by several logical pools cannot always be attributed: drain ambiguous instances before enabling finite execution-pool limits.

Use `node-ctl resource pressure` to distinguish raw pool pressure, outstanding recovery obligations and missing destination supply. A sandbox paused for balancing can remain paused when no eligible destination is available. Verify its full memory budget, startup allowance and destination supply before retrying; an admission rejection alone is not evidence that the artifact is corrupt. Follow the [pool recovery contract](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-resource.md#resource-pools) for the exact accounting and recovery conditions.

## Cluster failure domains

| Failure | Impact | Recovery |
|---|---|---|
| One registry member crashes. | Its logical shards lose one replica; writes continue only with quorum. | Quorum reads/read-repair catch up after recovery. Already routed sessions can continue without a fresh registry lookup, subject to their existing node/transport connections. |
| Entire registry is powered off, shard data retained. | Reserve/Place unavailable during outage. | Read retained data after quorum returns; node-link reconnects and converges. |
| Execution shards are irrecoverably lost. | Do not reconstruct live sandbox/build node execution state from a stale backup. | Recover provider data through its durability process. Live-node projection recovery and migration-token durable routes remain tracked by [#34](https://github.com/kuasar-sandbox/orchestrator/issues/34)/[#33](https://github.com/kuasar-sandbox/orchestrator/issues/33); report unavailable recovery explicitly until implemented. |
| Router crashes. | Connections through that replica break. | LB selects another stateless replica. |
| Placer crashes. | It leaves the ready set; cold placement fails over to another placer for the same group. | Hot routing is unaffected; Registry tries another candidate after Place timeout. |
| A compute node loses node-link. | Registry temporarily lacks its current view. | Node reconnects/reports. After node_dead_after, node_list expires it and placer stops selecting it; orphan cleanup uses group+sandbox_id. |

## Acceptance after recovery

Validate route synchronization, identity/credentials, resource admission, cold reads, command/file roundtrip and application results on each affected path. When process-state restoration is required, also check a known pre-pause in-memory value; matching files alone does not prove memory restoration. Reopen admission only after the required checks pass.

Check [restore eligibility](upgrading.md#restore-eligibility) before node migration or a version change. Delete test instances through their owner; do not remove shared parents or artifacts directly. See [deployment shutdown order](deployment.md#startup-and-shutdown-dependencies) for maintenance and [security](security.md#key-incidents) for key incidents.

<a id="retention-runbook"></a>
## Retention and generation retirement

The application/platform owner supplies the retained root inventory: canonical
published template IDs, paused snapshots, live instances, rollback/backup roots,
build outputs still needed after Build-row expiry, and intended retention deadlines.
The runtime/storage operator resolves each root's complete E/S memory/disk parent
closure and immutable kernel/runtime bindings using the component inspection
contracts. Store cannot infer this inventory from traffic or the newest generation.
[Store retirement](https://github.com/kuasar-sandbox/accelerator/blob/main/docs/store.md#administrative-commands),
[Manifest keys](https://github.com/kuasar-sandbox/accelerator/blob/main/docs/transfer.md#manifest-configyaml)
and [Node credential lifecycle](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-proxy.md#credential-model)
remain authoritative; this is the decision sequence, not a second GC protocol.

| Proposed deletion | Evidence required before deletion |
|---|---|
| Build record | Owner cleanup completed per Build Cancel/DELETE; separately retain needed canonical IDs. Deleting the row does not delete remote artifacts. |
| Local artifact or parent | Complete retained-root/parent inventory, no live owner dependency, and verified alternate durable source if retention continues. A semantic alias or leaf-only list is insufficient. |
| Store generation G1 | No retained root/parent depends on objects available only in G1. A G2 rollout merely changes default writes; listed G1 may still accept explicit admissions. Coordinate all writers/readers and drain inflight before physical deletion. |
| Old runtime/kernel | No retained live VM, template, snapshot, parent or rollback path needs that immutable identity; qualified restore with the retained bindings succeeds. |
| Old customer key | No retained encrypted artifact needs it, or every intended replacement has been separately created and fully recovery-tested with its own retained closure. A new API key or generation does not re-encrypt existing data. |

Before retirement, freeze the deletion set, reconcile new publications/retention
changes, verify from a cold reader against the durable source, and test restore of
representative retained roots. Record refs, identities, generations, key identifiers
(not secret bytes), owners and results. If the inventory is incomplete, any read/
restore fails, writes are still racing, or key provenance is uncertain, **stop**
and retain the data/key. `purge --generation` is destructive list removal plus
physical deletion, not a reference-aware dry run. Do not use it to discover whether
anything still depends on G1. After an approved retirement, verify retained roots
again and retain backups according to the operator's recovery policy.
