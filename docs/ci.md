[English](ci.md) | [简体中文](ci_zh.md)

# Continuous integration

## 1. Public caller and trusted control

The platform owns `ci-entry.yml`, `integration-tests.yml` and `integration-architecture.yml`. Component PR wrappers keep `ci-entry.yml@main`; GitHub resolves that trusted framework once per run. Every job, including admission, coordination, publication, finalization and cleanup, checks **the actual event repository before runner allocation**:

```yaml
if: github.event.repository.visibility == 'public' && github.event.repository.full_name == github.repository
```

A public workflow provider, candidate input or companion cannot authorize a private/internal/unknown actual caller. Such calls allocate no jobs and provide no successful acceptance evidence. Public callers may allocate capability-matched supplemental E2E groups under the trusted profile policy. Runner integration and provisioning are documented under [ci/runner](../ci/runner/README.md).

Component callers and platform forks use trusted-main `pull_request_target` control for `main` and `release/vMAJOR.MINOR.x`. Non-draft same-repository component PRs retain automatic admission. Fork admission still queries current active organization membership; `author_association` is not membership evidence. Admission validates current PR/base/head refs and the two parents of the integration commit, and writes `kuasar/ci-exact-head=pending` to that integration commit. Drafts, external forks and conflicts do not get a successful E2E result.

Same-repository platform PRs use `pull_request` and a local reusable workflow at the exact two-parent merge commit. This allows a framework change to validate its own prepare/run contract before it reaches main. Source, build, preparation and product execution receive read-only job tokens and no App secrets. All selected stages, including source/UFFD and performance, must pass before the integration result succeeds. Drafts allocate no product jobs. Component and fork callers retain their existing trusted admission/finalization.

The required `ci / finalize` job collects the complete platform PR result and rechecks the current source set before succeeding. It uses read-only API access; no separate commit-status publisher is needed.

An admission failure or deferred Draft must not complete the trusted finalizer successfully. The primary target branch ref is read again, as well as the PR metadata and ordered integration parents, so a cached PR response cannot validate an older base. Trusted finalization also reuses the current source-set validator to compare the effective companion declaration; unrelated prose edits do not invalidate the candidate. Each selected source/architecture stage must actually succeed; a skipped or neutral Actions check is not product validation. Server-side required-check configuration and the workflow result are separate controls and must be checked together. The [external contribution reception procedure](../CONTRIBUTING.md#ci-eligibility-and-external-contributions) describes non-member Fork handling; rerunning a rejected Fork does not change author membership. This contract adds no approval quota or particular review bot.

App keys and short-lived control tokens remain confined to trusted admission/finalization and release-control jobs. Product/helper builds, source checks, preparation and E2E never receive the App key. Candidate execution uses fresh hosted jobs or a fresh owned Workbench instance for a supplemental E2E group. Public release sources are retrieved by selected tag; candidate branch/PR refs are admitted against automatically observed identities and transported as fixed run artifacts. Checkouts retain no credentials; the narrowly scoped Actions token is used only in trusted API/download steps. Publish has its own job and write permission.

Atomic changes can declare reciprocal companions in the existing PR-body marker:

```text
<!-- kuasar-ci-companions
kuasar-sandbox/orchestrator#227
-->
```

A marker occurs at most once and lists each allowed repository once. Companions must be public, open, Ready PRs with current two-parent merge refs, exact base/head SHAs and heads belonging to an organization-repository branch. Admission, execution resolution and finalization recheck them. The primary status never replaces a companion's own actual-caller qualification. A body edit requires a new head or draft-to-ready event; compare the run's plan with the current marker. Remove merged companions and rerun the remaining candidates. Finalization succeeds only if every selected stage succeeds and the admitted source set is still current. Skipped/cancelled/failed stages cannot authorize a merge.

## 2. Exact baseline and affected products

`source` mode resolves one published, validated aggregate through the maintained `releases/daily-preview.yaml` for mainline, or `releases/release.yaml` for a maintenance line. Only that selection or its declared predecessor can recover an interrupted publication. The plan binds its tag commit, six independent unit versions/SHAs, release/asset IDs, sizes, digests and successful aggregate validation. New dual aggregates also bind their declared profiles and exact validated asset hashes in release notes.

When the maintained aggregate is not yet published, the explicit predecessor remains the validated binary baseline. For a complete schema 4 aggregate, ordinary source PRs keep non-candidate products, tests and helpers on that aggregate's selected owner tags; pending publication selections do not replace this qualified tuple. Admitted candidate and companion sources are still applied with their normal affected-product and consumer coverage. The separate exact-assets release gate validates the newly selected publication tuple before it becomes a baseline.

Historical capability cutovers retain the existing maintained-tag path when the authenticated aggregate evidence predates schema 4. Every differing tag must already have a complete dual-architecture publication with exact tag/commit, size and digest metadata. CI transports those exact tagged sources and rebuilds their products and consumers. Candidate manifests cannot select these tags or compatibility mode; incomplete publications, mixed requirement generations and requirement downgrades still fail. Runtime and kernel remain independent.

Missing baseline/ARM assets fail resolution with an explicit initialization requirement. The resolver never assembles component Latest versions or falls back to a full source build. Historical AMD64-only releases remain valid historical releases; they are not a dual baseline. See [first ARM initialization](release.md#first-arm-initialization).

Candidate and companion product inputs are compared against their baseline source revisions. A small explicit map selects standalone Go/native products and necessary linked/embedded products. Accelerator flatten changes rebuild `flatten-ctl` and the runtime that embeds it; `sandbox-init` changes rebuild the runtime used by guest tests. Runtime and vmlinux retain independent source identities and kernel selection reuses the existing Makefile input projection. Orchestrator `app/` and `config/` are product inputs. Documentation and test-only edits do not select component products.

Build may retrieve exact library sources needed by local Go replacements, without rebuilding every library repository's standalone binaries. Unmodified products remain byte-identical to downloaded baseline archives; candidate product hashes must equal this run's actual build outputs. This is not a claim that unrelated clean rebuilds are reproducible byte for byte.

Tarstream carrier changes also rebuild `sandbox-ctl` and `node-ctl` with the selected accelerator source. A newly encoded carrier must reach readers linked with the same library input; this does not select a sandboxer storage feature or change its source revision.

Sandboxer library changes likewise rebuild `node-ctl`, whose management and publication paths read checkpoint metadata. The orchestrator source selection remains unchanged. Working-set fixtures provide raw ext4 images through `diff_template` and leave active `diff` outputs absent; restore creates those outputs from the captured disk graph. Fresh active files follow the selected encryption policy, so local `off`/`auto` timing comparisons include active-file and carrier costs.

Product-impact selection also adds consumer tests: accelerator/runtime changes that rebuild `flatten-ctl` select telemetry cases, and sandboxer library changes that rebuild `node-ctl` select `orchestrator.resource-*`. These cases use the plan's exact test-owner revisions and capability-matched lanes; they do not select another main revision. Atomic harness/dependency fixes use the reciprocal companion declaration above to qualify their explicit joint source tuple.

### Documentation and manifest qualification before downloads

Source PR resolution compares the complete exact base, head and integration Git
trees for the primary and every admitted companion. The `docs` lane permits only
ordinary `100644` Markdown in `README{,_zh}.md`, `test/e2e/README{,_zh}.md`,
`docs/` and `test/e2e/guides/`. Platform also allows
`workbench/README{,_zh}.md`, `ci/runner/README{,_zh}.md` and
`test/demo/DEMO{,_zh}.md`. The `manifest` lane adds only platform's
`releases/release.yaml` and `releases/daily-preview.yaml`; it may include the same
allowed documentation and runs both checks. Any source, CI/workflow, release
tooling, test, runtime configuration, unknown path/repository or nonordinary file
in any candidate retains full CI. Truncated trees and empty changes also retain
full CI. Missing exact identities fail. Deleting a maintained manifest fails its
pair validation; a symlink sibling cannot supply a manifest.

Policy and checking code come from the platform's trusted baseline, including
the platform companion's base when present. A candidate cannot add itself to the
allowlist. A baseline without the new manifest policy keeps manifest changes on
full CI. The candidate workflow still tests its own contracts through the full
source gate, so the implementation can be tested before activation. Classification
precedes baseline assets, source archives and build environments. No candidate
code executes in either light lane.

The shared `ci/check_docs.py` rules check UTF-8, local links and anchors,
reciprocal pairs and English defaults. English-only documents remain valid.
Changed documents and unchanged documents referring to renamed/deleted/changed
targets are checked; unrelated documentation debt is not expanded into this
focused check. External access and translation completeness still require review.

Manifest qualification parses both maintained files at the exact head and merge
with the existing release parser: fields, unit tags, Stable/Preview versions,
predecessor ordering, pair consistency and maintenance-line ownership must be
valid. The existing aggregate reader authenticates declared published predecessors
and any already published selection. A historical AMD64-only predecessor keeps
its original x86 validation scope; this comparison reference is not promoted to
a usable dual baseline. Already published current selections and new declared
Workbench releases retain their complete architecture/asset requirements.
Published components use the existing
exact tag/Release/dual-architecture asset checks. Preview source and dependency
bindings remain required; a changed published dependency may reuse a consumer
only when the existing linked-input projection is identical in complete source
trees. A new component tag, tag awaiting publication or draft is recorded as a
pending publication prerequisite, with its release source branch and selected
dependencies. Reuse against a pending dependency records the outstanding linked-
input verification. The release coordinator still freezes future source commits
and satisfies these prerequisites. This is release-plan validation, never a claim
that a future tuple has passed E2E; no component publication is dispatched here.

The existing finalizer accepts deliberate heavy-job skips only after successful
light evidence matches this exact source set, trusted policy, run and attempt,
and current PR/base/head/companion admission is rechecked. Missing, cancelled,
failed, empty or stale evidence cannot pass. Evidence retains the manifest plans,
published identities and pending prerequisites. Aggregate release `exact-assets`
always retains its complete E2E and Workbench acceptance. Record actual
`docs`/`manifest`/`full` Actions timings before claiming savings. The implementation
PR itself follows full CI because it changes executable policy/workflows.
Rollback restores full scheduling without relaxing admission or required checks.


The Go EROFS runtime requires only init, envd and flatten-ctl inside its bundle.
New aggregate binaries omit mkfs.erofs. Historical archives may retain that
tool, and legacy runtimes still require its embedded bytes to match the selected
outer binary. The trusted reader selects this contract from flatten-ctl's linked
external go-erofs module record without executing the binary or requiring Go.
Host runtime packing, independent fsck and native control tests retain their tools.

### Capability-based E2E allocation

Plan schema 4 separates the required case selection from execution placement.
It discovers headers from all six selected owner trees and freezes case hashes,
requirements, the trusted `ci/integration/runner-capabilities.json` configuration,
its digest, executor image IDs, run/attempt and deterministic groups. Existing
schema 2/3 plans and published results keep their original ARM selection and
Workbench qualification meanings.

A profile declares architecture, runner labels, executor, resource budget,
priority, enabled state and evidence for every capability. `supported` satisfies
a requirement; `unsupported` records a known gap; `unknown` does neither. Among
profiles satisfying every requirement, public runners take precedence, then
priority and profile ID break ties. Every supplemental placement records at
least one known unsupported requirement for **each** enabled public candidate.
An unknown-only public candidate blocks allocation. Disabled or unavailable
resources do not make required cases optional. Case metadata cannot set labels,
images, commands, priority or credentials. Runtime-only containers cannot satisfy
`systemd` or `journald`, even when the enclosing host can.

Groups are formed by profile, suite and effective runtime. Their disjoint union
must equal the ordinary suite-selected cases, excluding credentialed OBS unless
explicitly requested. Public build, helper, prepare, source and performance jobs
retain their allocation. Both source and exact-assets modes prepare the union
once on public infrastructure. Preparation exports its already built runtime
image and binds the compressed archive digest; execution verifies the archive
and image config before loading it. No execution group rebuilds products.

Supplemental groups use the existing Workbench system launcher. Host Actions
steps verify and transport fixed inputs, then run the existing public E2E entry
inside the owned instance. Storage, snapshot and ARM image groups retain a
nested runtime-only container with no compilers or component source trees.
Each group gets a fresh temporary download/prepared/result directory and a unique
Workbench instance name, including on a reused self-hosted machine. Diagnostics
are uploaded before retained task files are deleted. Runner credentials and host
Docker sockets are not mounted into Workbench.
Startup capability checks run in the effective runtime, and results bind actual
runner name/OS/architecture/environment, run/attempt, profile/configuration,
prepared provenance and executor image. The architecture and final gates require
every nonempty group; failure, cancellation, missing results or identity changes
cannot become success. Empty groups allocate no runner and claim no E2E.
Partial reruns reuse successful artifacts only for the same immutable plan and
workflow run. Receipts retain their actual producer attempt; a new failed or
cancelled execution prevents collection even if an earlier result exists.

The existing same-repository framework PR entry resolves companion declarations
from its admitted event using read-only public metadata. It uses the same live
merge/head/base and branch checks before execution and final acceptance; caller-
supplied companion records remain forbidden on that entry.

Requirements must first be present in the selected component versions. A mixed
old/new source set fails instead of treating absent declarations as empty.
Wholly historical inputs retain the historical plan path; after a schema 4
baseline, requirement generation cannot be downgraded. Before activating a
capability marked `unknown`, record real product acceptance through the existing
prepare/run flow and update its evidence in the trusted configuration. There is
no recurring capability-probe matrix or automatic failure fallback. Combined
E2E coverage does not change artifact-only or single-instance native-full
Workbench qualification. See [runner integration](../ci/runner/README.md#capability-e2e-runners)
and [case declarations](../test/README.md#static-execution-requirements).

## 3. Independent architecture lifecycles

```text
resolve plan ── x86 build ── x86 prepare ── native x86 shards ── x86 result ─┐
             ├─ native ARM build ── ARM prepare ── native ARM shards ── ARM result ─┼─ required results
             └─ source unit/race/vet and UFFD checks ─────────────────────┘
```

Each architecture builds on its native Runner (`ubuntu-24.04` or `ubuntu-24.04-arm`) through `.github/actions/workbench`, using the existing Makefiles and native recipes. The trusted resolver selects a published, qualified Workbench once per run and binds both registry digests, image config IDs and the framework SHA. Later jobs verify that selection, including the actual image architecture and release/source labels. Ordinary PRs still build only the admitted product delta and required helpers. Each lane prepares its immutable inputs once and starts E2E when its own preparation finishes. Shards, artifacts and results retain architecture/run identities and are explicitly collected.

Build mode uses the existing `workbench/workbench` launcher: ordinary UID, read-only root, no capabilities, no host Docker socket or KVM. Private sources are mounted at `/src`; the trusted framework is read-only at `/inputs/release`. `HOME=/work/home`, the short disk-backed `TMPDIR=/build/t`, Go/Cargo state and `/build/native-cache` are task-owned. Explicit CPU/memory budgets reach Go, Cargo, CMake and native `make` recipes; CPU quota alone is not treated as `nproc`. Publisher credentials stay in host orchestration. Trusted publisher validators and archive readers use separate uncached Workbench invocations before host validation/publication.

Go/native ELF and kernel `Image` checks, static-link and package-layout assertions remain required. Build helpers and packaging tools are native to the selected architecture. Workbench itself is built directly on the native Runner by the existing producer; it never builds itself.

`prepare-artifacts.py` checks archive path/type/mode/ownership, digests, required products and embedded runtime identity before composition. Different architectures never share an extraction directory. Source mode overlays all six exact test-owner trees and assembles one flat case directory with namespaced low-level libraries; exact-assets mode consumes that layout from the platform package. Both paths reject legacy owner runners and duplicate case IDs. The plan records test revision per owner and the trusted framework separately. Guest init identity comes from the validated runtime payload; a selected candidate init must match that payload, while an unchanged baseline runtime keeps its original embedded bytes.

New aggregate manifests select only owner/unit tags. Cases, runtime libraries, helper source and guides come from those selected sources; guest-runtime follows the runtime tag and kernel inputs follow the independent vmlinux tag. The resolver checks repository, tag, commit and tree against actual clean sources and records the facts in `source-records.json`. Those commit identities are provenance, never independent input selectors.

Source CI admits the existing PR merge refs and trusted branch refs against automatically observed identities, then transports the fixed Git trees in `source-inputs.tar`. Build, helper and required source-check jobs restore that artifact. Noncandidate owners use their selected unit tags. Candidate and companion tests run even when the related-input projection selects no products and every product byte comes from the published aggregate. The product projection and independent kernel projection are unchanged.

For an owner without an admitted candidate, the selected tag must retain every case ID in the validated baseline. Missing cases fail explicitly and require a reviewed new-tag selection; source CI must not silently reduce coverage.

Historical aggregate consumers retrieve the manifest and packages by aggregate tag. They preserve original independent test provenance. New publication bindings embed the canonical validation plan, owner case mapping and source records alongside exact package digests. For an older binding without ownership, the reader requires its original retained integration-plan artifact, verifies its canonical identity against `plan_id`, and cross-checks all bound identities and assets. Missing or ambiguous evidence fails explicitly; filenames cannot establish historical ownership.

The product contract is prebuilt products → `e2e prepare` → `<suite>.<case>.sh` → the shared public `e2e run`. The full filename is the case ID; its first segment is one of `basic`, `storage`, `image`, `network`, `sandbox`, `snapshot`, `orchestrator`, `builder` or `telemetry`. Internal CI shards group these exact files by suite. The plan records the selected files and architecture exclusions before execution.

Source/helper build derives the complete helper inventory and recipes from the admitted owner declarations, then binds the contract to the input plan. The generic builder runs each owner's recipe with the fixed dependency workspace and rejects missing or extra outputs. Internal binary names, package paths and build targets remain component responsibilities. Preparation verifies the expected contract, architecture, source identity, bytes and safe modes; it exposes declared environment aliases and `E2E_HELPERS_DIR` without compiling anything. Required case capabilities, including the static no-libc cgroup probe, remain checks rather than the complete allowed inventory. Release packaging carries both helper architectures with the same owner-tag evidence. The same earlier build stage fetches the complete Demo SDK wheel closure for Python 3.12 from `test/demo/requirements.lock`; all versions and wheel hashes are fixed. Release packages include both architecture wheelhouses. Preparation consumes those binaries and supplies manifest archives, the guest flatten fixture, resolved image IDs/digests, orchestrator base images and the Demo SDK installed with `--no-index --find-links` and `--require-hashes` from its local wheelhouse. Missing or changed wheels fail before installation. Provenance binds package names, versions, wheel hashes, lock identity and the installed file tree. It invokes the same public preparation entry used for a downloaded release and verifies that existing product/test inputs were not changed. Capture helper tests remain in the independent orchestrator source gate. The prepared workspace contains `bin/`, `test/`, `fixtures/`, `images/`, license/material files and `provenance.json`. Real Build, flatten, snapshot, publish and restore operations remain in focused product cases.

The E2E job checks out only the trusted executor, downloads its prepared target and verifies all files/modes before and after execution. It does not check out component sources or invoke product Go/Cargo/kernel builds. Each shard owns a short disk-backed mutable directory; Unix sockets, direct I/O, Docker configuration and performance output stay outside immutable inputs. Source-dependent connector/sandboxer/orchestrator unit/race/vet, real pinned-BPF stats, ENOSPC, Collector/usage harness regressions and UFFD source benchmarks run in a separate required source job. Source-mode x86 sandboxer/platform also retain the existing A/B/C/D `off/auto × cold/warm` working-set smoke in an independent performance job using the same prepared product bytes.

The complete source gate runs in an independent Workbench system instance on native x86, including the compilers used by its mixed unit/race/vet and real privileged checks. The trusted host restores the sources bound to `plan.test_revisions` from the admitted run artifact; the instance receives a private copy at `/src` and no host Docker socket, credentials or ordinary build cache. Existing owner scripts and Make targets remain authoritative at each exact test pin, including published older layouts. They execute as an ordinary user inside that instance so unreadable-file and privilege-refusal checks still run. The source process and its children retain the Hosted affinity policy: select allowed CPUs using the declared `KUASAR_BUILD_JOBS` budget, within the instance CPU quota. Its private sudo configuration preserves the original explicit privilege transitions; only the existing TAP/netns performance fixtures run through `sudo make test-perf-tools`. The user can access only the instance's Docker socket, and no host account or policy is changed. Real `sudo`, systemd, BPF, mount namespaces, UFFD and private Docker retain their original flags and assertions. TAP and network namespaces are required before the gate starts. Results retain framework/test/image identities, individual commands, exits and timings; failure evidence is collected through the owned-instance cleanup. Product, independent helper and release builds use ordinary-UID build mode. Host bootstrap retains orchestration and product-execution prerequisites, while the Workbench producer builds directly on its native runner.

CI supplies root privileges and the trusted tool path directly when launching selected prepared cases. Only explicit prepared inputs cross `sudo`, including the private state directory; execution does not consult a generated owner-runner registry.

All nonempty lanes prepare inside a runtime-only container with no Go, Rust or C/C++ compilers and no component source trees. The full storage and snapshot suites also execute there, covering non-KVM and KVM contracts; applicable native ARM image cases use the same boundary. Cases requiring systemd run on the selected native host or in the selected Workbench system instance. Trusted preflight and result records bind the immutable runtime image ID and the verified absence of compilers/source trees; static zero-case lanes do not count as product acceptance. Container inputs are read-only and mutable case/output directories are separate.

Failures in `snapshot.read-recovery.sh` retain the case, phase, failing line and bounded fixed-vocabulary error evidence in the job log before cleanup. Diagnostics preserve the original test exit status.

The existing `make test-release-tools` entry also runs frozen source-set release validation when the source CI context or either materialization document is present. Both `.source-plan.json` and `.source-checks.json` are then required. It accepts the original `source` and `exact-assets` plans and binds their unchanged plan ID, clean trees and existing Go workspace. Components retain their selected tag identities; candidates retain their admitted refs. In `exact-assets`, the unpublished platform source instead binds the original repository, commit and tree to the staged selection, fixed run input, materialization receipt and helper declaration. It verifies these inputs before invoking each selected component's original `make test-release` serially; a platform candidate selects all five components. It also executes the selected owners' declared native helper recipes, verifies exact outputs and records commands, exits, declarations, dependency identities and binary digests under `KUASAR_CI_DIR`. Sources and workspace inputs must remain unchanged afterward. Authenticated historical helper evidence is retained as historical, without claiming a new recipe execution. This native source regression does not replace architecture build, prepare or E2E validation.

Standalone `make test-release-tools` keeps its platform tooling scope and explicitly reports when no frozen source-set run was requested. For a local, already materialized source set, use `make test-release-source-set ORG=<fixed-source-root> RELEASE_CHECK_OUTPUT=<fresh-evidence-directory>` with the existing Go workspace and toolchain environment. This explicit entry always requires both admission documents and never retrieves new sources.

### Source check groups

`run-source-checks.py --group all` remains the default complete entry. Group `A`
retains platform contracts (the full `make test-ci-tools test-release-tools`
invocation), privileged performance contracts, selected connector/sandboxer
scripts, accelerator fixtures and the existing UFFD check. Group `B` contains
the selected orchestrator script. Selection, command arguments, working
directories and dependency closure are identical to `all`; B is not scheduled
when orchestrator is unselected. Each scheduled group uses a separate hosted
machine and fresh Workbench system instance with the original 2 CPU/8 GiB
budget, private sources, Go workspace, temporary directories and build/module
caches. No source cache is restored or shared between groups.

The reusable workflow keeps `source_check_groups: false` as its compatibility
default. The PR callers in `ci.yml` and `ci-entry.yml` select A/B after rollout
acceptance; the aggregate caller retains `all` until its own final-workflow
benefit and cost are measured. Set either PR caller back to false to restore
`all`; no target, resource budget or required check changes.

Two paired real Actions measurements used identical selected component sources,
case inventories, complete commands and immutable Workbench image within each
pair; the candidate trees differed only in the caller's group flag. Both pairs
were fresh cold builds without a restored writable source cache, including the
repeat. These first-attempt samples measure final workflow feedback and total
job minutes, not the historical command model or later fault/retry costs.

| Pair (all / A+B) | Final feedback | Total job minutes | Feedback / cost change |
| --- | --- | --- | --- |
| [38543260895](https://github.com/kuasar-sandbox/kuasar-sandbox/actions/runs/38543260895/attempts/1) / [38543242866](https://github.com/kuasar-sandbox/kuasar-sandbox/actions/runs/38543242866/attempts/1) | 37:43 / 25:28 | 99.467 / 104.550 | -32.48% / +5.11% |
| [38549209231](https://github.com/kuasar-sandbox/kuasar-sandbox/actions/runs/38549209231/attempts/1) / [38549216882](https://github.com/kuasar-sandbox/kuasar-sandbox/actions/runs/38549216882/attempts/1) | 44:09 / 27:06 | 108.983 / 110.450 | -38.62% / +1.35% |

The later cancellation of pair 2's grouped run is a separate negative test;
its current run conclusion must not replace the successful first-attempt
measurement. Revalidate current-source correctness and all six caller paths,
including failure and partial reruns, before completing rollout. These SOURCE
measurements do not establish aggregate or Workbench-group savings.

Both schedules bind results to the exact plan, original command coverage,
Workbench image, successful cleanup, and current Actions producer job and
artifact IDs. Artifact names include run, attempt and group; they never
overwrite another attempt. A failed, cancelled, missing or duplicate producer
blocks finalization. A successful earlier attempt is reusable only while it
is the latest producer for that group and still matches the current plan;
rerunning that producer invalidates its older artifact. A/B cannot share an
instance or machine. Rollback selects `all` without removing these checks or
altering release selection and required-check rules.

Actions may give a carried successful job a new ID in a partial rerun while retaining its original execution timestamps, runner and steps. The shared reader traces such a view back only to the matching latest preceding successful execution; a genuinely newer running, failed or cancelled producer still invalidates old evidence. The same rule covers source groups and Workbench runtime groups, ARM, staged inputs and the validation job.

### Workbench runtime groups

The reusable workflow and aggregate dispatch expose `workbench_runtime_groups`,
defaulting to false. The default retains the full x86 runtime job and Hosted ARM
artifact-only qualification. Experimental x86 groups are A (`basic`, `image`,
`orchestrator`, `sandbox`, `snapshot`) and B (`builder`, `network`, `storage`,
`telemetry`). Membership derives from this plan's exact ordinary case inventory;
unknown suites fail, coverage must be complete and disjoint, and the existing
`storage.obs.sh` exclusion remains. Suite order and state stay within one group.
Each independent hosted machine and private empty Workbench instance prepares
**all** ordinary offline inputs, then runs its assigned cases with the unchanged
2 CPU/8 GiB budget, preflight, egress, isolation, timing, disk and cleanup checks.

Grouped results use explicit `workbench-groups-v1` / `native-grouped` evidence.
They retain both executions, common exact source/assets/image identities,
complete coverage and immutable run/attempt/group artifacts. Current successful
producer jobs, ARM evidence, stage/plan inputs and the final validation job must
agree. An unchanged earlier successful attempt may be reused; rerunning any
bound producer invalidates the old validation. Missing, duplicate, failed,
cancelled or shared-instance evidence cannot authorize publication.

Reader compatibility must be merged before grouped production: the publisher
loads `bind-validation.py` from **main**. Registry publication, release notes,
baseline/artifact resolution, Hosted selection and Preview recovery retain
legacy full and ARM results alongside grouped evidence. Public downloads keep
the same assets and per-architecture selection. Release-body and complete
registry-reservation limits are unchanged. Repeated comparable real Actions
runs, including failure/rerun injections, queue/preparation/execution/final
feedback and total job minutes, must justify activation. Local or simulated
metadata is not Actions acceptance. Without stable final benefit at acceptable
cost, keep `all`. Rollback disables production while retaining readers for any
published grouped evidence.

For repeated same-source qualification, aggregate dispatch also accepts
`validation_only=true` (default false). It still requires main, the exact current
selection and an unpublished version, and runs all original build/runtime/source
checks, publication binding, body limits and registry-input verification. It
stops before registry/Release writes and retains this run's artifacts under their
existing expiry for failure/rerun tests. Its `Validate aggregate <version> @<sha>`
identity and per-run `aggregate-validation` concurrency key let independent `all`
and grouped validations coexist with ordinary publication. Validation runs,
including the former `[validation only]` suffix titles, never count as active
publications, publication retries, recovery candidates or successful publication
baselines. Ordinary publication and delete-preview retain their shared
`aggregate-mutation` lock. Validation checks that the version is unpublished both
before and after the archive, publication-body and registry-input consumers.
The second check runs immediately before success reporting and fails if the API
then reports a published release. This is a final API observation, not atomic
exclusion of publication after that observation. Compare `all` and grouped inputs on the
same selection while it remains the exact current branch HEAD; do not substitute
these pre-publication timings for real publication end-to-end benefit. Finish the selected release through the normal
publication mode, and never rerun producers behind already published grouped
evidence merely to obtain a benchmark.

## 4. Daily and Stable

`exact-assets` is callable only by the actual public platform aggregate workflow. Its plan binds the exact committed manifest and complete staged asset set (platform, component archives, Workbench images and SHA256SUMS). It selects no product or helper rebuilds. Each target uses the same download/compose/public-prepare/public-run/result primitives as PR mode and consumes the prebuilt helper packages from the exact staged platform archive. The separate required source checks remain outside artifact E2E.

Publish verifies both successful architecture results against the staged digests and publishes the original archives unchanged. Component material/license/source identity validation, trusted publisher notes and version immutability remain required. The exact ARM selection and capability groups are recorded before execution and in the aggregate validation binding. Historical non-KVM evidence remains limited to its original scope.

## 5. Native cache

`ci/native-cache/native-cache.sh restore-or-build` handles:

- guest `vmlinux`;
- guest `envd`;
- RocksDB headers and `librocksdb.a`;
- patched `cloud-hypervisor`.

Cache entries live at `$KUASAR_NATIVE_CACHE_ROOT/v3/<arch>/<component>/<input-hash>/`. The Workbench action transfers native entries, Go modules/build cache and Cargo registry/Git/material downloads through Actions cache, subject to the caller's runtime cache permissions. Keys include architecture, exact candidate producer identity and declared build coverage. The candidate namespace binds the actual PR, companion, product, test/helper and kernel inputs. Both exact keys and restore prefixes separate helper builds, selected product sets and release units, so an immutable partial entry cannot occupy another build coverage key.

After restoration, the cached Workbench action runs `go clean -testcache` before owner commands. This expires previous Go test results while retaining compilation and module caches, so restored success cannot replace the current job's tests. The command and its exit status are recorded in the Workbench receipt; failure prevents owner execution and still runs the existing cleanup.

Actions cache storage and access belong to the caller repository and ref under GitHub's cache rules; equal keys do not share storage across repositories. A main-branch dispatch with candidate inputs remains candidate-scoped. Trusted cache writes require clean, exact public source commits on the corresponding main histories, admitted before source execution; that decision is retained outside candidate mounts. Existing trusted main/release invocations provide the write path when their runtime cache scopes permit it, using their existing permissions. Publisher executables never consume candidate or build caches.

Aggregate helpers restore clean Git trees from the same selected unit tags as products, using the fixed source artifact. Runtime release freezes the same host receipt before installing verified prebuilt assets, so generated binaries and notices are not mistaken for modified source. The original asset validation and actual toolchain/recipe cache keys still apply.

Component `pull_request_target` jobs have read-only cache access. An attempted save can report `cache write denied: token has no writable scopes` while the cache action step itself succeeds. That outcome is not a successful save. Workbench receipts record the requested cache identity and restore outcome; they do not confirm a write. Cache acceptance requires an actual successful save in the intended repository/ref, followed by restoration in a fresh job and validation of the exact products and packaging materials. A writable cache in another repository or event does not establish that path.

Native keys cover recipe, patch/configuration, upstream content, architecture, actual compiler/linker/ABI and meaningful flags. Workbench image and framework identities remain in provenance without treating a daily image label as a compiler input. Synthetic import and kernel build defaults use reproducible identities/dates; explicit overrides remain significant. Fixed container paths retain meaningful Cargo source and linker identities. Random task directories do not enter the Actions cache path version. Entries are published with checksums and atomic rename, then validated again before restore. A miss builds using the original recipe; a matching damaged entry fails instead of silently rebuilding or repairing itself.

EROFS writing uses the Go generator; project patches, mkfs builds and their native-cache component are retired. Runtime bundle packing uses the host mkfs preinstalled in Workbench. Trusted reader tooling builds only fsck/dump and does not export mkfs. Existing artifacts remain readable.

Envd retains its matching Go workspace source context. Cloud Hypervisor retains the patched source tree, original build report, linker map, Cargo lock/metadata/source manifests and license inputs; Cargo registry/Git and verified release-material downloads are restored alongside native entries. A restored package is validated against those exact materials without substituting old evidence or silently recompiling. Incomplete legacy cache schemas miss. Repository patch files remain from the admitted source set, and standalone package validators retain access to the exact selected Git objects.

Build and restore of the same key hold an entry lock. Each component retains its four most recently used keys by default. Reclamation only removes entries beyond the protection period whose locks can be acquired without blocking. Cache tests:

```bash
make -C kuasar-sandbox test-ci-tools
```

## 6. Hosted prerequisites and evidence

Ordinary component/helper compilation, build-time tests and release packaging use the selected Workbench. Host orchestration supplies source admission, artifact transport and publication. Trusted EROFS/runtime readers are exported from a separate Workbench build to compiler-free prepare/E2E jobs; those jobs retain their existing runtime dependencies and architecture/capability declarations.

`artifact-x86` supplies Docker/systemd/cgroup v2/KVM/UFFD/netns/BPF; `artifact-arm` supplies the declared non-KVM execution profile. The x86 VM bootstrap keeps its existing per-job KVM udev/group setup and verifies actual KVM/UFFD access. Missing required capabilities and failed assertions fail the job. Workbench receipts bind image acquisition, resources, commands, exits, cache scope and cleanup; GitHub job/step timings provide restore/save and transport costs. Only owned instances are stopped. Diagnostics reject escaping symlinks and special files before host artifact upload; cache/output state is removed after evidence is retained.

Run evidence uses `integration-plan`, per-architecture `integration-provenance`, per-shard `integration-shard`, `integration-source-result`, both `integration-architecture-result` and final `integration-validation` artifacts, each suffixed with run ID/attempt. Provenance includes baseline asset identities, product origin/hashes, exact source/test/framework revisions, embedded payloads, actual tools/native keys, helper and fixture hashes, modes and predeclared case selection. Independent working-set results use `integration-performance`; clean preparation/execution results include their verified runtime environment. Result records include selected cases, exit codes, timings and the prepared provenance digest. Aggregate cleanup removes large build/prepared/stage transfers, retaining validation metadata for seven days. Raw credential-bearing runtime state is not uploaded.

Local contract checks: `make test-ci-tools test-release-tools test-perf-tools` with trusted EROFS readers and `KUASAR_RUNTIME_READER`; component release/workflow and fixture checks retain their existing entries. Developer `make test-e2e RELEASE_DIR=... E2E_WORKDIR=...` wraps the same public prepare/run pair without compiling products or helpers. Source build, helper and performance targets remain independent. Offline/fixture checks and native prechecks do not establish actual public-runner acceptance.

## 7. Initial coverage and rollout evidence

This is the single initial coverage ledger for #152. The Chinese guide links here for scope, reasons, evidence and follow-up. Selected cases require actual successful results.

| Architecture / owner | Predeclared scope | Public execution evidence | Gap / next step |
| --- | --- | --- | --- |
| x86_64 / all six owners | Existing complete owner entries, split core/sandboxer/orchestrator; OBS excluded | [Core](https://github.com/kuasar-sandbox/kuasar-sandbox/actions/runs/35751885794/job/106831237865), [sandboxer](https://github.com/kuasar-sandbox/kuasar-sandbox/actions/runs/35751885794/job/106831237847) and [orchestrator](https://github.com/kuasar-sandbox/kuasar-sandbox/actions/runs/35751885794/job/106831239335) passed with prepared assets | Each admitted PR must retain its own plan, results and applicable working-set smoke; final rollout evidence is tracked in [#152](https://github.com/kuasar-sandbox/kuasar-sandbox/issues/152) |
| x86_64 / source checks | Required unit/race/vet, pinned-BPF stats, ENOSPC, Collector source regressions, UFFD | [Separate required source job passed all six checks](https://github.com/kuasar-sandbox/kuasar-sandbox/actions/runs/35751885794/job/106828635736), including the UFFD source benchmark | Retained for applicable PR owners; PR working-set smoke remains required |
| aarch64 / accelerator | Existing cache/store/rolling/manifest and port-lease non-KVM suite | [Native ARM execution passed](https://github.com/kuasar-sandbox/kuasar-sandbox/actions/runs/35751885794/job/106829880307) | Keep the declared non-KVM scope |
| aarch64 / guest-runtime | Existing non-KVM flatten/OCI suite | [Native ARM execution passed](https://github.com/kuasar-sandbox/kuasar-sandbox/actions/runs/35751885794/job/106829880307) | Keep the declared non-KVM scope |
| aarch64 / connector, sandboxer, orchestrator, platform | Product identity/composition only; no selected owner E2E | [Composition and provenance passed](https://github.com/kuasar-sandbox/kuasar-sandbox/actions/runs/35751885794/job/106829377176); exclusions fixed before execution | No independently selected ARM non-KVM owner entry; VM/KVM/restore/Builder/cluster parity is follow-up |
| both / credentialed OBS or real cloud | Not selected | No cloud claim | Separate environment and evidence required |

[Aggregate run 35751885794, attempt 1](https://github.com/kuasar-sandbox/kuasar-sandbox/actions/runs/35751885794) passed both declared profiles and published [release-v0.1.5-preview.20260922.4](https://github.com/kuasar-sandbox/kuasar-sandbox/releases/tag/release-v0.1.5-preview.20260922.4) from source/framework `69c26d2e9d7a494b3463e42c3b8c20ac1119de4f`. Its release validation binding records plan `00969ac8d0bd389b78101ece51675c9315ce2d985beaf6c18b36a8169dcc700b`, independent owner test pins, both provenance digests and successful shard results. All fourteen published assets match the tested staged digests; anonymous SHA256SUMS readback passed. All six component units were reused, and eighteen historical release/tag/asset identities remained unchanged. Standard x86 jobs used `ubuntu-latest`; native ARM used `ubuntu-24.04-arm`.

[PR #129](https://github.com/kuasar-sandbox/kuasar-sandbox/pull/129) activated this shared workflow at `integration-tests.yml` in commit `dea0bc66ae766caf47f3c6684a7f426b79b730ba`. The temporary artifact alias and source orchestration are removed; component wrappers still use `ci-entry.yml@main`. Its [required pre-merge run](https://github.com/kuasar-sandbox/kuasar-sandbox/actions/runs/35756663913) passed source E2E, UFFD and working-set smoke using the previous trusted framework. That source qualification remains separate from artifact PR acceptance: each real caller records its resolved framework SHA, baseline/delta plan, prepared-workspace execution, both architecture results and applicable working-set smoke. [#152](https://github.com/kuasar-sandbox/kuasar-sandbox/issues/152) records final caller evidence before rollout closure.

Completed Public source acceptance: [platform #159](https://github.com/kuasar-sandbox/kuasar-sandbox/actions/runs/35660952525), [accelerator #144](https://github.com/kuasar-sandbox/accelerator/actions/runs/35662919390), [connector #64](https://github.com/kuasar-sandbox/connector/actions/runs/35662687375), [sandboxer #267](https://github.com/kuasar-sandbox/sandboxer/actions/runs/35640791998), [orchestrator #408](https://github.com/kuasar-sandbox/orchestrator/actions/runs/35654577585), and [guest-runtime #74](https://github.com/kuasar-sandbox/guest-runtime/actions/runs/35640824293). These runs exercised the source path during initialization; the table tracks the separate artifact rollout.

Accelerator, connector, sandboxer and orchestrator are Public; visibility and anonymous source/asset access were read back after the authorized cutover. Their actual Public source CI passed before normal merges, alongside platform and guest-runtime. The historical capability disposition is closed in [#82](https://github.com/kuasar-sandbox/kuasar-sandbox/issues/82#issuecomment-5765674410): those fixtures used per-invocation loopback/local-only authority, the original instance was destroyed, and copied fixtures create independent local instances. Ordinary review and required checks remain in force.

## 8. See also

- [Release](release.md): selection, first ARM initialization, assets and publishing.
- [Deployment](deployment.md): runtime services and capabilities.
- [Runner operations](../ci/runner/README.md): existing persistent infrastructure.
- [Test quick start](../test/QUICKSTART.md): developer E2E prerequisites.

Supplemental self-hosted E2E restores its fixed sparse executor from a small same-run capsule produced by the trusted resolve job. Git packs the original commit, tree and blob objects into a fresh repository with no copied remote configuration or credentials; export attributes cannot rewrite or omit tracked bytes. The resolver passes the archive SHA-256 and expected Git tree through workflow outputs. Before any capsule code runs, bootstrap verifies that hash and the exact workflow SHA; restoration then checks the commit/tree, all Git objects, clean tracked files and unchanged sparse scope. Symlinks, gitlinks, special modes, oversized archives and unsafe paths fail closed. The pinned official download action has a five-minute bootstrap limit and restoration has three minutes. Hosted E2E retains its normal checkout. This avoids repeated Git connections and lazy fetches on supplemental runners; prepared products still use the bounded local downloader below.

Artifact acquisition in the architecture jobs uses the local same-run download action. Each attempt has a 600-second wall-clock deadline starting before metadata resolution and covering response headers, transfer, compressed SHA-256 verification and extraction. At most three attempts use fresh signed URLs, connections and private directories, with 15/30-second backoff. Artifact IDs, producing jobs, sizes and digests are frozen after the first successful metadata response. Authentication, certificate, identity, digest, archive and unknown errors stop immediately; only typed transient transport failures and deadlines retry. Cancellation kills and reaps the worker before cleanup, and a verified directory is promoted only after the complete selection succeeds. The step limit is 35 minutes; existing overall job deadlines still apply. Logs contain phase, attempt and elapsed time, never runtime tokens or signed URLs. The Node entry receives the runner-provided same-run identity and invokes Python standard-library code, without installing packages or widening workflow permissions.
