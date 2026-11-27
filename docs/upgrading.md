[English](upgrading.md) | [简体中文](upgrading_zh.md)

# Upgrades and restore compatibility

This guide owns version composition, restore qualification and upgrade procedures. Component specifications continue to define E/S formats, VMM/Guest ABI and artifact bindings. Shared-storage reachability alone does not certify a destination for an existing instance.

## Choose the state that must survive

| Path | Preserved state | Qualification before upgrade |
| --- | --- | --- |
| Create a new instance from a template | Template contents; new instance identity | Target version reads the template and completes launch, application readiness and data access |
| Memory restore from S | Captured processes, memory and disk state | Compatible CPU/architecture, VMM, kernel/runtime, formats, full parent closure and node ownership |
| Cold start from E, or explicitly from the E referenced by S | Saved disks; processes start again | Target supports E and its workload/disk graph; cold boot derives a new C0 with the target kernel/runtime identities, and the application reinitializes |
| Rebuild a template | Explicitly migrated application data and new software environment | Build/ready/create acceptance with the new inputs and a separate application-data migration |

On a paused instance, `connect(memory=false)` explicitly gives up old process memory. Connect on a running instance stays idempotent and does not restart it; Pause first when a restart is intended. It does not bypass format, identity or dependency validation. When S compatibility cannot be established, qualify a cold-start or rebuild path instead of editing metadata to impersonate compatibility.

## Upgrade and rollback procedure

1. Record the current aggregate tag, component/Runtime/VMLinux versions, configurations, live instances, paused roots, templates and rollback deadlines. Decide which workloads require process continuity and which tolerate a restart.
2. Prepare the complete target combination in a new directory using [installation](installation.md). Keep old directories, keys, artifacts and immutable runtime/kernel inputs. Do not overwrite files referenced by live or retained state.
3. On an isolated node, test new creates, Build, commands/files/ports, pause, S restore and any required E cold start using the actual network, storage, architecture and CPU pair.
4. Use the Node Group, optional importer and request-selector mechanisms to manage eligible cluster destinations. Stop new placements on a node before maintenance; an online node is not automatically compatible with all old snapshots.
5. Complete the supported pause/export/import or in-place retention workflow for preserved instances. Verify commit success and the dependency closure before stopping services from consumers to providers. SIGTERM is not an instruction to snapshot every VM.
6. Activate the target combination, verify business paths and reopen admission gradually. Record failures, skipped checks and remaining old-version dependencies.
7. On failed acceptance, stop new placements and restore still-compatible old configuration and program inputs. Never assume an old reader accepts formats written by a newer version; use retained old roots or supported export/migration procedures where needed.

Retire old dependencies only after migration and the rollback retention period, using [retention](operations.md#retention-runbook). This is an operating procedure, not a claim of automatic rolling upgrades or arbitrary live migration between versions.

## Adopt the delivered Guest runtime behavior

New runtime bundles include their private systemd/executor paths and restore
notification support. Keep each referenced runtime immutable: pointing an old S
at a new bundle does not upgrade the processes already captured in memory. To
adopt a new runtime or initialization policy, qualify an E cold start or rebuild,
then capture new S state and retain the old bundle for any remaining old roots.

Use the native direct/systemd launch choice and application restore notification
from the [Guest runtime guide](https://github.com/kuasar-sandbox/guest-runtime/blob/main/docs/using-guest-runtime.md).
Do not replace the shipped Guest init flow with image-wide service disabling or
ad hoc address-flush scripts as a default. The platform restores its owned NIC
state; applications still need to reconnect external services, refresh time-based
credentials and handle their own restart/restore semantics. Test these application
steps before enabling traffic on the new template.

## Cluster refactor state boundary

The shared Registry backend uses **record schema v2**. SQLite, PostgreSQL and
FoundationDB share its business implementation; choosing a new backend does not
copy data. The Node-owned Group/Registry-selection protocol also changes the
participating binary contract. Qualify the selected matching release as a whole;
there is no promised mixed-version rolling transition for these experimental
states. Snapshot readers must accept the produced runtime annotation and format
before new artifacts are created.

For disposable pre-release clusters, use a deliberate rebuild: stop admission,
finish/remove test-owned work with the old version, stop old roles and retain
needed evidence, then use new Registry/Node state with the matching release.
Reinstall tenant roots and Groups and create new instances from compatible
retained canonical artifacts. New instances receive new identities. Do not treat
an empty namespace as migrated ownership, and do not edit SID/ready rows.

| State | Backup/restore boundary |
|---|---|
| Registry database | Use the selected backend's consistent backup procedure; record schema and namespace. No API exports/imports execution rows or reconstructs them from Node reports. |
| Node DB and encryption key | Retain each Node's own durable lifecycle/Group/credential state and its matching key. Restoring Registry alone does not rewind running Nodes. |
| Tenant roots and Group inputs | Retain roots and explicit Group/import-source definitions independently; importer input alone does not recover running execution ownership. |
| Artifacts/runtime | Retain canonical IDs, full dependency closure, content keys and exact immutable runtime bundles needed by those snapshots. |

For a same-schema database restore, keep admission closed, stop processes that
can write/cache the restored history, and demonstrate consistent Node/Registry
ownership before reopening. If required ownership is lost, do not promise
same-ID recovery. The [backend schema and restore guide](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/cluster-storage.md#schema-and-upgrade-boundary)
provides the exact supported boundary; no automatic old-schema converter or
execution-state import tool is available.

## Versions and validation

Components have independent versions and releases. An aggregate version selects exactly six release units: four component archives, the guest runtime and the guest kernel. Aggregate preparation validates the selected component assets and SHA256; acceptance tests the staged bytes under each architecture's declared profile. The release package records the exact version combination to avoid mixing assets from different dates or aggregate versions.

Qualification is profile-specific: native x86 runs its required KVM cases, while native ARM runs its declared profile with explicit hardware exclusions. A hosted ARM Workbench `artifact-only` result is not evidence of VM or offline system execution. Compare the recorded architecture, profile and executed cases with the target deployment before upgrading; see [release asset validation](release.md#8-release-asset-validation).

These mechanisms support version pinning, fault diagnosis and repeatable validation in production. Operators still need production implementations and configuration for TLS, persistent backends, backups, monitoring, network policy and capacity.

<a id="restore-eligibility"></a>
## Cross-node restore eligibility and failure boundaries

A portable reference describes content; it does not certify every destination.
Before allowing a target node, the deployment owner must validate all of:

- **Architecture and CPU:** same native guest architecture and a destination CPU
  feature set compatible with the captured VM. Qualify the actual source/target
  CPU pair; neither an architecture label nor a shared store proves compatibility.
- **VMM and snapshot schema:** compatible selected patched VMM and sandboxer
  formats. [Artifact compatibility](https://github.com/kuasar-sandbox/sandboxer/blob/main/docs/artifacts.md#compatibility)
  rejects old schemas rather than silently migrating or cold-booting them.
- **Kernel/runtime:** memory restore retains S's original E/C0; the running kernel
  is already captured in S, so restore does not read an external kernel file or
  its identity sidecar. The external runtime PMEM backing must match E's digest
  scheme and declared Bundle footer digest, without a full payload scan. A matching
  configured runtime may have any basename; the captured basename is only the
  single-sibling fallback hint, not content identity or a directory-search rule.
  Keep runtime bytes immutable while referenced, retain kernel inputs needed for
  cold starts/rollback, and follow [runtime selection](https://github.com/kuasar-sandbox/guest-runtime/blob/main/docs/using-guest-runtime.md#node-upgrades). In cluster
  mode, annotated SNP placement additionally requires the captured basename and
  digest in that same Node's published runtime inventory. This is stricter than
  the lower-level configured-runtime check; retain and publish the required
  bundle before permitting selection.
- **Complete durable closure:** S, referenced E, memory/disk parents and their
  immutable objects remain resolvable with the same canonical identities and
  named locations. Test from the destination without relying on warm cache hits.
- **Keys and ownership:** retain the customer keys needed by every retained
  encrypted layer and the node/cluster's required credentials and ownership state.
  Shared ciphertext without keys is not recoverable state.
- **Network and admission:** destination provider/topology and the current
  attachment-generation contract must be compatible; gateway return/policy and
  Proxy routing must work. Restore keeps captured capacity/device topology;
  destination allocatable policy and resource admission still apply. A portable
  artifact is not a reservation or permission to reuse the old host's TAP FD.

[Sandboxer memory restore](https://github.com/kuasar-sandbox/sandboxer/blob/main/docs/runtime.md#restore)
and [Node artifact lifecycle](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/sandbox-lifecycle.md#capture-and-pause)
own the checks and permitted overrides. Qualify with file/process-state assertions,
new reads outside the warm set, network roundtrip and application reconnect checks.
Failure at any preflight means stop placement, preserve artifacts and correct the
binding; do not change captured metadata to force acceptance.

| Incident | What can and cannot be recovered |
|---|---|
| Proxy/Router/connector serve process crash | Their documented reconnect/reopen paths can recover if owner state and underlying resources survive. Confirm route sync, identity and traffic before admission. This is not a VM checkpoint operation. |
| Sandbox/VMM process or whole node loss | Uncaptured memory is lost. Surviving local state is not automatically a complete portable checkpoint. Restore only a successfully committed artifact after eligibility and ownership checks. |
| Previously paused artifact, original node unavailable | Recovery is possible only when durable closure, keys, bindings, network, admission and the applicable Node/cluster ownership protocol remain available. Shared storage supplies only one of these prerequisites. |
| Registry execution state lost | Current protocol cannot reconstruct execution ownership from route events or import backup execution rows. [Registry recovery limits](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/cluster.md#registry-state-ownership-and-disaster-recovery-boundaries) describe unimplemented recovery/bootstrap work. Provider data or a portable snapshot does not authorize fabricating Registry rows or claiming same-ID recovery. |
