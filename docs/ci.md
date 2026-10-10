[English](ci.md) | [简体中文](ci_zh.md)

# Continuous integration

## 1. Public caller and trusted control

The platform owns `ci-entry.yml`, `integration-tests.yml` and `integration-architecture.yml`. Component PR wrappers keep `ci-entry.yml@main`; GitHub resolves that trusted framework once per run. Every hosted job, including admission, coordination, publication, finalization and cleanup, checks **the actual event repository before runner allocation**:

```yaml
if: github.event.repository.visibility == 'public' && github.event.repository.full_name == github.repository
```

A public workflow provider, candidate input or companion cannot authorize a private/internal/unknown actual caller. Such calls allocate no hosted jobs and provide no successful acceptance evidence. There is no private ARM queue, private adaptation, larger runner or paid fallback. Existing runner provisioning remains documented under [ci/runner](../ci/runner/README.md); this workflow does not alter those services.

Component callers and platform forks use trusted-main `pull_request_target` control for `main` and `release/vMAJOR.MINOR.x`. Non-draft same-repository component PRs retain automatic admission. Fork admission still queries current active organization membership; `author_association` is not membership evidence. Admission validates current PR/base/head refs and the two parents of the integration commit, and writes `kuasar/ci-exact-head=pending` to that integration commit. Drafts, external forks and conflicts do not get a successful E2E result.

Same-repository platform PRs use `pull_request` and a local reusable workflow at the exact two-parent merge commit. This allows a framework change to validate its own prepare/run contract before it reaches main. Source, build, preparation and product execution receive read-only job tokens and no App secrets. All selected stages, including source/UFFD and performance, must pass before the integration result succeeds. Drafts allocate no product jobs. Component and fork callers retain their existing trusted admission/finalization.

The required `ci / finalize` job collects the complete platform PR result and rechecks the current source set before succeeding. It uses read-only API access; no separate commit-status publisher is needed.

An admission failure or deferred Draft must not complete the trusted finalizer successfully. The primary target branch ref is read again, as well as the PR metadata and ordered integration parents, so a cached PR response cannot validate an older base. Trusted finalization also reuses the current source-set validator to compare the effective companion declaration; unrelated prose edits do not invalidate the candidate. Each selected source/architecture stage must actually succeed; a skipped or neutral Actions check is not product validation. Server-side required-check configuration and the workflow result are separate controls and must be checked together. The [external contribution reception procedure](../CONTRIBUTING.md#ci-eligibility-and-external-contributions) describes non-member Fork handling; rerunning a rejected Fork does not change author membership. This contract adds no approval quota or particular review bot.

App keys and short-lived control tokens remain confined to trusted admission/finalization and release-control jobs. Product/helper builds, source checks, preparation and E2E never receive the App key. Candidate execution uses fresh standard jobs. Anonymous exact-SHA public source retrieval uses credential-free checkouts; the narrowly scoped Actions token is used only in trusted API/download steps. Publish has its own job and write permission.

Atomic changes can declare reciprocal companions in the existing PR-body marker:

```text
<!-- kuasar-ci-companions
kuasar-sandbox/orchestrator#227
-->
```

A marker occurs at most once and lists each allowed repository once. Companions must be public, open, Ready PRs with current two-parent merge refs, exact base/head SHAs and heads belonging to an organization-repository branch. Admission, execution resolution and finalization recheck them. The primary status never replaces a companion's own actual-caller qualification. A body edit requires a new head or draft-to-ready event; compare the run's plan with the current marker. Remove merged companions and rerun the remaining candidates. Finalization succeeds only if every selected stage succeeds and the admitted source set is still current. Skipped/cancelled/failed stages cannot authorize a merge.

## 2. Exact baseline and affected products

`source` mode resolves one published, validated aggregate through the maintained `releases/daily-preview.yaml` for mainline, or `releases/release.yaml` for a maintenance line. Only that selection or its declared predecessor can recover an interrupted publication. The plan binds its tag commit, six independent unit versions/SHAs, release/asset IDs, sizes, digests and successful aggregate validation. New dual aggregates also bind their declared profiles and exact validated asset hashes in release notes.

Missing baseline/ARM assets fail resolution with an explicit initialization requirement. The resolver never assembles component Latest versions or falls back to a full source build. Historical AMD64-only releases remain valid historical releases; they are not a dual baseline. See [first ARM initialization](release.md#first-arm-initialization).

Candidate and companion product inputs are compared against their baseline source revisions. A small explicit map selects standalone Go/native products and necessary linked/embedded products. Accelerator flatten changes rebuild `flatten-ctl` and the runtime that embeds it; `sandbox-init` changes rebuild the runtime used by guest tests. Runtime and vmlinux retain independent source identities and kernel selection reuses the existing Makefile input projection. Orchestrator `app/` and `config/` are product inputs. Documentation and test-only edits do not select component products.

Build may retrieve exact library sources needed by local Go replacements, without rebuilding every library repository's standalone binaries. Unmodified products remain byte-identical to downloaded baseline archives; candidate product hashes must equal this run's actual build outputs. This is not a claim that unrelated clean rebuilds are reproducible byte for byte.

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

Historical aggregate consumers retrieve the manifest and packages by aggregate tag. They preserve original independent test provenance. New publication bindings embed the canonical validation plan, owner case mapping and source records alongside exact package digests. For an older binding without ownership, the reader requires its original retained integration-plan artifact, verifies its canonical identity against `plan_id`, and cross-checks all bound identities and assets. Missing or ambiguous evidence fails explicitly; filenames cannot establish historical ownership.

The product contract is prebuilt products → `e2e prepare` → `<suite>.<case>.sh` → the shared public `e2e run`. The full filename is the case ID; its first segment is one of `basic`, `storage`, `image`, `network`, `sandbox`, `snapshot`, `orchestrator`, `builder` or `telemetry`. Internal CI shards group these exact files by suite. The plan records the selected files and architecture exclusions before execution.

Source/helper build produces target zot, versitygw, custom Proxy, telemetry probe and sandboxer usage probe binaries, plus the static no-libc x86_64/aarch64 cgroup probe. The orchestrator helper set also includes `node-ctl-runner-test`, compiled with `go test -c ./cmd/node-ctl` from the admitted orchestrator source identity and its selected dependency workspace. It is a test-only executable, not a production product. Preparation verifies its architecture, source pin and hash, and exports `NODE_CTL_RUNNER_TEST_BINARY` as the immutable `fixtures/bin/node-ctl-runner-test` path; neither preparation nor E2E compiles it. Release packaging includes both helper architectures with derived owner source identities and hashes. The same earlier build stage fetches the complete Demo SDK wheel closure for Python 3.12 from `test/demo/requirements.lock`; all versions and wheel hashes are fixed. Release packages include both architecture wheelhouses. Preparation consumes those binaries and supplies manifest archives, the guest flatten fixture, resolved image IDs/digests, orchestrator base images and the Demo SDK installed with `--no-index --find-links` and `--require-hashes` from its local wheelhouse. Missing or changed wheels fail before installation. Provenance binds package names, versions, wheel hashes, lock identity and the installed file tree. It invokes the same public preparation entry used for a downloaded release and verifies that existing product/test inputs were not changed. Capture helper tests remain in the independent orchestrator source gate. The prepared workspace contains `bin/`, `test/`, `fixtures/`, `images/`, license/material files and `provenance.json`. Real Build, flatten, snapshot, publish and restore operations remain in focused product cases.

The E2E job checks out only the trusted executor, downloads its prepared target and verifies all files/modes before and after execution. It does not check out component sources or invoke product Go/Cargo/kernel builds. Each shard owns a short disk-backed mutable directory; Unix sockets, direct I/O, Docker configuration and performance output stay outside immutable inputs. Source-dependent connector/sandboxer/orchestrator unit/race/vet, real pinned-BPF stats, ENOSPC, Collector/usage harness regressions and UFFD source benchmarks run in a separate required source job. Source-mode x86 sandboxer/platform also retain the existing A/B/C/D `off/auto × cold/warm` working-set smoke in an independent performance job using the same prepared product bytes.

The complete source gate runs in an independent Workbench system instance on native x86, including the compilers used by its mixed unit/race/vet and real privileged checks. The trusted host only fetches admitted `plan.test_revisions`; the instance receives a private copy at `/src` and no host Docker socket, credentials or ordinary build cache. Existing owner scripts and Make targets remain authoritative at each exact test pin, including published older layouts. They execute as an ordinary user inside that instance so unreadable-file and privilege-refusal checks still run. Its private sudo configuration preserves the original explicit privilege transitions; only the existing TAP/netns performance fixtures run through `sudo make test-perf-tools`. The user can access only the instance's Docker socket, and no host account or policy is changed. Real `sudo`, systemd, BPF, mount namespaces, UFFD and private Docker retain their original flags and assertions. TAP and network namespaces are required before the gate starts. Results retain framework/test/image identities, individual commands, exits and timings; failure evidence is collected through the owned-instance cleanup. Product, independent helper and release builds use ordinary-UID build mode. Host bootstrap retains orchestration and product-execution prerequisites, while the Workbench producer builds directly on its native runner.

CI supplies root privileges and the trusted tool path directly when launching selected prepared cases. Only explicit prepared inputs cross `sudo`, including the private state directory; execution does not consult a generated owner-runner registry.

All nonempty lanes prepare inside a runtime-only container with no Go, Rust or C/C++ compilers and no component source trees. The full storage and snapshot suites also execute there, covering non-KVM and KVM contracts; applicable native ARM image cases use the same boundary. Cases requiring host systemd retain their native-host job. Trusted preflight and result records bind the immutable runtime image ID and the verified absence of compilers/source trees; static zero-case lanes do not count as product acceptance. Container inputs are read-only and mutable case/output directories are separate.

Failures in `snapshot.read-recovery.sh` retain the case, phase, failing line and bounded fixed-vocabulary error evidence in the job log before cleanup. Diagnostics preserve the original test exit status.

## 4. Daily and Stable

`exact-assets` is callable only by the actual public platform aggregate workflow. Its plan binds the exact committed manifest and staged fourteen-file asset set (platform + twelve component archives + SHA256SUMS). It selects no product or helper rebuilds. Each target uses the same download/compose/public-prepare/public-run/result primitives as PR mode and consumes the prebuilt helper packages from the exact staged platform archive. The separate required source checks remain outside artifact E2E.

Publish verifies both successful architecture results against the staged digests and publishes the original archives unchanged. Component material/license/source identity validation, trusted publisher notes and version immutability remain required. ARM's non-KVM scope is recorded before execution and in the aggregate validation binding; it is never presented as full VM parity.

## 5. Native cache

`ci/native-cache/native-cache.sh restore-or-build` handles:

- guest `vmlinux`;
- `mkfs.erofs` and `fsck.erofs`;
- guest `envd`;
- RocksDB headers and `librocksdb.a`;
- patched `cloud-hypervisor`.

Cache entries live at `$KUASAR_NATIVE_CACHE_ROOT/v3/<arch>/<component>/<input-hash>/`. The Workbench action transfers native entries, Go modules/build cache and Cargo registry/Git/material downloads through Actions cache, subject to the caller's runtime cache permissions. Keys include architecture, exact candidate producer identity and declared build coverage. The candidate namespace binds the actual PR, companion, product, test/helper and kernel inputs. Both exact keys and restore prefixes separate partial source/helper builds from their selected product set and full-manifest builds, so an immutable partial entry cannot occupy the full-build key.

After restoration, the cached Workbench action runs `go clean -testcache` before owner commands. This expires previous Go test results while retaining compilation and module caches, so restored success cannot replace the current job's tests. The command and its exit status are recorded in the Workbench receipt; failure prevents owner execution and still runs the existing cleanup.

Actions cache storage and access belong to the caller repository and ref under GitHub's cache rules; equal keys do not share storage across repositories. A main-branch dispatch with candidate inputs remains candidate-scoped. Trusted cache writes require clean, exact public source commits on the corresponding main histories, admitted before source execution; that decision is retained outside candidate mounts. Existing trusted main/release invocations provide the write path when their runtime cache scopes permit it, using their existing permissions. Publisher executables never consume candidate or build caches.

Aggregate helpers materialize clean Git trees at their independently selected test revisions. Runtime release freezes the same host receipt before installing verified prebuilt assets, so generated binaries and notices are not mistaken for modified source. The original asset validation and actual toolchain/recipe cache keys still apply.

Component `pull_request_target` jobs have read-only cache access. An attempted save can report `cache write denied: token has no writable scopes` while the cache action step itself succeeds. That outcome is not a successful save. Workbench receipts record the requested cache identity and restore outcome; they do not confirm a write. Cache acceptance requires an actual successful save in the intended repository/ref, followed by restoration in a fresh job and validation of the exact products and packaging materials. A writable cache in another repository or event does not establish that path.

Native keys cover recipe, patch/configuration, upstream content, architecture, actual compiler/linker/ABI and meaningful flags. Workbench image and framework identities remain in provenance without treating a daily image label as a compiler input. Synthetic import and kernel build defaults use reproducible identities/dates; explicit overrides remain significant. Fixed container paths retain meaningful Cargo source and linker identities. Random task directories do not enter the Actions cache path version. Entries are published with checksums and atomic rename, then validated again before restore. A miss builds using the original recipe; a matching damaged entry fails instead of silently rebuilding or repairing itself.

EROFS keys include Libgcrypt/Libgpg-error/uuid pkg-config metadata, target compiler/tool bytes, actual local source archive bytes (or the expected digest for a pinned URL), and a bounded compiler/static-link probe. The probe tracks consumed headers, including forced includes, and the archives/startup objects actually selected through flags, sysroots and library search paths. A source URL or filename is a locator, not content identity. Logical workspace file labels are relocatable; meaningful compiler and sysroot flag values remain significant. An unpinned URL cannot authorize a shared cache; use a pinned URL or a local archive.

Optional `guest-runtime/native-deps/deps/erofs-patches` material, ordered `series` and `deps/erofs-recipe.sh` enter the key. Older source sets without those files are supported, including the previous OpenSSL recipe's actual target link probe. Adding, changing or removing inputs invalidates the key. The Workbench image installs `libgcrypt20-dev libgpg-error-dev uuid-dev` and retains `libssl-dev` for already-admitted older source sets. openEuler 24.03-LTS-SP4's `libgcrypt-1.10.2-4` and `libgpg-error-1.47-1` source RPMs explicitly disable static libraries; their devel packages alone are insufficient. The [runner provider](../ci/runner/README.md#install) builds those pinned distro-patched sources with at most two jobs, installs only the static archives and validated source/build/relink/license catalogs, and verifies warm reuse and template-to-slot copies. Runtime packaging validates the same pinned catalogs; Ubuntu keeps the installed-package material path.

EROFS entries require the matching `.erofs-recipe` stamp, binary hashes, source archive/tree, external link dependencies, maps, objects, source licenses and relink inputs. Envd retains its matching Go workspace source context. Cloud Hypervisor retains the patched source tree, original build report, linker map, Cargo lock/metadata/source manifests and license inputs; Cargo registry/Git and verified release-material downloads are restored alongside native entries. A restored package is validated against those exact materials without substituting old evidence or silently recompiling. Incomplete legacy cache schemas miss. Repository patch files remain from the admitted source set, and standalone package validators retain access to the exact selected Git objects.

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
