[English](ci.md) | [简体中文](ci_zh.md)

# Continuous integration

## 1. Public caller and trusted control

The platform owns `ci-entry.yml`, `integration-tests.yml` and `integration-architecture.yml`. Component PR wrappers keep `ci-entry.yml@main`; GitHub resolves that trusted framework once per run. Every hosted job, including admission, coordination, publication, finalization and cleanup, checks **the actual event repository before runner allocation**:

```yaml
if: github.event.repository.visibility == 'public' && github.event.repository.full_name == github.repository
```

A public workflow provider, candidate input or companion cannot authorize a private/internal/unknown actual caller. Such calls allocate no hosted jobs and provide no successful acceptance evidence. There is no private ARM queue, private adaptation, larger runner or paid fallback. Existing runner provisioning remains documented under [ci/runner](../ci/runner/README.md); this workflow does not alter those services.

`pull_request_target` accepts `main` and `release/vMAJOR.MINOR.x`. Non-draft same-repository PRs retain automatic admission. Fork admission still queries current active organization membership; `author_association` is not membership evidence. Admission validates current PR/base/head refs and the two parents of the integration commit, and writes `kuasar/ci-exact-head=pending` to that integration commit. Drafts, external forks and conflicts do not get a successful E2E result.

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
             ├─ ARM cross build ── ARM prepare ── native ARM shards ── ARM result ─┼─ required results
             └─ source unit/race/vet and UFFD checks ─────────────────────┘
```

Both builds run in separate `ubuntu-24.04` x86 jobs/workspaces using existing Makefiles and native-cache recipes. Each architecture builds its selected products once and prepares its immutable inputs once. One lane's E2E starts after its own preparation; it does not wait for the other build. Execution uses `ubuntu-24.04` or standard `ubuntu-24.04-arm`, with architecture/shard/run identifiers in concurrency, artifacts and results. Every shard and both architecture results are explicitly collected; no matrix output can overwrite the other result.

RocksDB and cache-ctl share explicit `CROSS_PREFIX` and target CGO CC/CXX. Rust keeps the provided toolchain and uses its target std/linker. ARM vmlinux uses the kernel `Image` header; Go/native payloads require Linux ELF64 for the selected architecture. EROFS uses target static dependencies and target pkg-config resolution. Host `BUILD_MKFS_EROFS`, Runtime writer/readers and Go packaging helpers remain host-native. No `NO_ROCKSDB` or environment compiler byte allowlist is used.

`prepare-artifacts.py` checks archive path/type/mode/ownership, digests, required products and embedded runtime identity before composition. Different architectures never share an extraction directory. A candidate test owner replaces its complete tree, including helpers and removed-file checks; all other tests come from the selected platform package. The plan records test revision per owner and the trusted framework separately.

Preparation supplies manifest Docker archives, the guest flatten fixture, resolved container image IDs/digests, orchestrator base-image fixtures and target helper binaries (zot, versitygw, custom Proxy, telemetry probe). The prepared workspace contains `bin/`, `test/`, `fixtures/`, `images/`, license/material files and `provenance.json`. Helpers/fixtures are preparation inputs; business Build, flatten, snapshot, publish and restore operations remain in their original E2E cases.

The E2E job checks out only the trusted executor, downloads its prepared target and verifies all files/modes before and after execution. It does not check out component sources or invoke product Go/Cargo/kernel builds. Each shard owns a short disk-backed mutable directory; Unix sockets, direct I/O, Docker configuration and performance output stay outside immutable inputs. Source-dependent connector/sandboxer/orchestrator unit/race/vet, real pinned-BPF stats, ENOSPC and Collector regressions and UFFD source benchmarks run in a separate required source job. Source-mode x86 sandboxer/platform also retain the existing A/B/C/D `off/auto × cold/warm` working-set smoke using the same prepared product bytes.

## 4. Daily and Stable

`exact-assets` is callable only by the actual public platform aggregate workflow. Its plan binds the exact committed manifest and staged fourteen-file asset set (platform + twelve component archives + SHA256SUMS). It selects no product rebuilds. Each target uses the same download/compose/prepare/profile/result primitives as PR mode; helper preparation may compile test tools from the exact selected test revisions. The separate required source checks remain outside artifact E2E.

Publish verifies both successful profile results against the staged digests and publishes the original archives unchanged. Component material/license/source identity validation, trusted publisher notes and version immutability remain required. ARM's non-KVM scope is recorded before execution and in the aggregate validation binding; it is never presented as full VM parity.

## 5. Native cache

`ci/native-cache/native-cache.sh restore-or-build` handles:

- guest `vmlinux`;
- `mkfs.erofs` and `fsck.erofs`;
- guest `envd`;
- RocksDB headers and `librocksdb.a`;
- patched `cloud-hypervisor`.

Cache entries live at `$KUASAR_NATIVE_CACHE_ROOT/v2/<arch>/<component>/<input-hash>/`. The public workflow sets this root inside each disposable build job. Hosted caches are local reuse only and are not uploaded to Actions cache or artifacts. The input hash covers build scripts, patches/configuration, upstream digests, architecture, Go/Cargo/C/C++ toolchains and pkg-config resolution. Entries are published through staging, checksums and atomic rename. Descriptor, payload and tar paths are checked again before restoring a hit. Corrupt entries fail rather than being repaired in place.

EROFS keys include Libgcrypt/Libgpg-error/uuid pkg-config metadata, target compiler/tool bytes, actual local source archive bytes (or the expected digest for a pinned URL), and a bounded compiler/static-link probe. The probe tracks consumed headers, including forced includes, and the archives/startup objects actually selected through flags, sysroots and library search paths. A source URL or filename is a locator, not content identity. Logical workspace file labels are relocatable; meaningful compiler and sysroot flag values remain significant. An unpinned URL cannot authorize a shared cache; use a pinned URL or a local archive.

Optional `guest-runtime/native-deps/deps/erofs-patches` material, ordered `series` and `deps/erofs-recipe.sh` enter the key. Older source sets without those files are supported, including the previous OpenSSL recipe's actual target link probe. Adding, changing or removing inputs invalidates the key. Hosted native profiles install `libgcrypt20-dev libgpg-error-dev uuid-dev` and retain `libssl-dev` for already-admitted older source sets. openEuler 24.03-LTS-SP4's `libgcrypt-1.10.2-4` and `libgpg-error-1.47-1` source RPMs explicitly disable static libraries; their devel packages alone are insufficient. The [runner provider](../ci/runner/README.md#install) builds those pinned distro-patched sources with at most two jobs, installs only the static archives and validated source/build/relink/license catalogs, and verifies warm reuse and template-to-slot copies. Runtime packaging validates the same pinned catalogs; Ubuntu keeps the installed-package material path.

EROFS retains one optional `bin/<arch>/.erofs-recipe` v2 stamp with both output hashes and actual external compiler/link dependencies, both link maps and their EROFS object/archive inputs, and source `LICENSES`, `AUTHORS` and `COPYING`. Restoring an identical recipe can reuse the outputs without an extracted source tree. Older caches without the stamp remain readable and rebuild on the next recipe check. Repository patch files remain from the admitted source set; cache restore does not replace them. Runtime patch-material validation uses the selected commit's local Git objects; those objects must be available to standalone validators. Actual target copyright/notices and source/relink inputs remain required for real release packaging.

Build and restore of the same key hold an entry lock. Each component retains its four most recently used keys by default. Reclamation only removes entries beyond the protection period whose locks can be acquired without blocking. Cache tests:

```bash
make -C kuasar-sandbox test-ci-tools
```

## 6. Hosted prerequisites and evidence

Bootstrap keeps the environment Go/Rust versions. Cross setup adds target packages and the matching Rust target to disposable x86 jobs; it does not upgrade compilers or alter runner services. Host EROFS readers use the existing pinned recipe and Runtime payload reader. Native caches, source trees and compiler caches remain job-local and are not uploaded.

`artifact-build`/`artifact-cross` supply native/cross prerequisites; `artifact-prepare` supplies host readers and fixture tools; `artifact-x86` supplies Docker/systemd/cgroup v2/KVM/UFFD/netns/BPF; `artifact-arm` supplies the selected non-KVM tools only. `source` retains the required source benchmark environment. The x86 VM bootstrap keeps its narrow per-job KVM udev/group fix, checks actual unprivileged KVM/UFFD access, and retains TUN/vhost-vsock ACLs. Missing selected capabilities fail. Build CPU affinity and Go/Cargo jobs stay bounded by available CPU/memory.

Run evidence uses `integration-plan`, per-architecture `integration-provenance`, per-shard `integration-shard`, `integration-source-result`, both `integration-architecture-result` and final `integration-validation` artifacts, each suffixed with run ID/attempt. Provenance includes baseline asset identities, product origin/hashes, exact source/test/framework revisions, embedded payloads, actual tools/native keys, helper and fixture hashes, modes and predeclared profile. Result records include selected cases, exit codes, timings and the prepared provenance digest. Aggregate cleanup removes large build/prepared/stage transfers, retaining validation metadata for seven days. Raw credential-bearing runtime state is not uploaded.

Local contract checks: `make test-ci-tools test-release-tools test-perf-tools` with trusted EROFS readers and `KUASAR_RUNTIME_READER`; component release/workflow and fixture checks retain their existing entries. Developer `make test-e2e` still prepares source products/helpers and invokes the owner suites; it is separate from the hosted artifact executor. Offline/fixture checks and native prechecks do not establish actual public-runner acceptance.

## 7. Initial coverage and rollout evidence

This is the single initial coverage ledger for #152. Update the evidence column with exact public run URLs and SHAs during rollout; an unexecuted selected case remains pending, not passed. Chinese readers: 本表同时记录首轮覆盖、原因、真实证据和后续事项。

| Architecture / owner | Predeclared scope | Evidence at implementation | Gap / next step |
| --- | --- | --- | --- |
| x86_64 / all six owners | Existing complete owner entries, split core/sandboxer/orchestrator; OBS excluded | Artifact/workflow/release contracts pass locally; actual public E2E pending | Run every actual public caller; record run/SHA and failures |
| x86_64 / source checks | Required unit/race/vet, pinned-BPF stats, ENOSPC, Collector source regressions, UFFD | Sandboxer and orchestrator source checks passed on isolated native precheck; public source/UFFD job pending | Actual standard-runner check remains required |
| aarch64 / accelerator | Existing cache/store/rolling/manifest and port-lease non-KVM suite | Architecture/composition fixtures pass; native ARM pending | Run existing suite on public standard ARM |
| aarch64 / guest-runtime | Existing non-KVM flatten/OCI suite | Architecture/embedded identity fixtures pass; native ARM pending | Run existing suite on public standard ARM |
| aarch64 / connector, sandboxer, orchestrator, platform | Product identity/composition only; no selected owner E2E | Exclusions fixed in plan before execution | No independently selected ARM non-KVM owner entry; VM/KVM/restore/Builder/cluster parity is follow-up |
| both / credentialed OBS or real cloud | Not selected | No cloud claim | Separate environment and evidence required |

The four component Public cutovers follow the existing #82 readiness records and #128 migration work, with scoped review of new content and real credential/distribution blockers. Public-only CI runs after actual visibility read-back. A guard-skipped private run cannot qualify a component. Preserve ordinary review/protections; no public proxy or protection bypass. Historical test-capability disposition still needs confirmed evidence before the authorized cutover; implementation tests alone do not resolve it.

## 8. See also

- [Release](release.md): selection, first ARM initialization, assets and publishing.
- [Deployment](deployment.md): runtime services and capabilities.
- [Runner operations](../ci/runner/README.md): existing persistent infrastructure.
- [Test quick start](../test/QUICKSTART.md): developer E2E prerequisites.
