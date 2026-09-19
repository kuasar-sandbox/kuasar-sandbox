[English](ci.md) | [简体中文](ci_zh.md)

# Continuous integration

## 1. Overview

The project repository owns the trusted CI control plane and the shared execution workflows. The five component repositories retain only event triggers and parameter wrappers. All PR wrappers continue to reference `.github/workflows/ci-entry.yml` from project `main`; GitHub resolves that trusted revision once per run. Guest release bootstrap checkouts pin a reviewed platform commit. That entry calls `.github/workflows/integration-tests.yml` from the same resolved platform revision. Common admission, runner initialization, source caching, native caching, full E2E and exact released-asset validation are not duplicated across repositories.

The execution workflow has two explicit modes:

- `source`: validates the triggering repository's PR integration commit, an optional exact companion-PR set and the other sources resolved for the target platform branch;
- `exact-assets`: validates release archives already downloaded and verified by the aggregate workflow, without rebuilding them.

## 2. Source mode

Each repository's trusted wrapper receives `pull_request_target` events without executing workflows supplied by the candidate repository. It accepts only PRs targeting `main` or `release/vMAJOR.MINOR.x`. Non-draft same-repository PRs are admitted automatically. For fork PRs, a trusted control job queries current organization membership using a read-only App token; only an `active` owner/member is admitted automatically. Public-repository admission and finalization use GitHub-hosted runners. The control job also re-queries the current PR, validates GitHub's generated two-parent integration commit and sets `kuasar/ci-exact-head` to `pending` on that commit. Event `author_association` is not the source of organization membership. Draft PRs run only admission/finalize control steps, not full E2E, and retain a `pending` exact-head status. Marking a draft ready causes a new event to run full Integration E2E. External forks, conflicts and events whose inputs have changed are rejected.

The other repositories' revisions are resolved through the GitHub REST API from the refs selected for the target platform branch. The six release units are resolved independently, including runtime and vmlinux; the source workspace uses the runtime unit for its single `guest-runtime` checkout, as detailed below. The candidate repository's revision is replaced with its admitted integration commit. An ordinary PR declares no companions and retains single-candidate behavior. Atomic cross-repository changes that cannot build against the other repositories' `main` can declare one another in both PR bodies:

```text
<!-- kuasar-ci-companions
kuasar-sandbox/orchestrator#227
-->
```

The marker may occur at most once. Each nonempty line must be an allowed `owner/repository#PR`. A repository may appear only once, and a PR cannot reference its triggering repository. Empty, duplicate and unclosed blocks are rejected. A companion must retain `refs/pull/N/merge`; the integration commit's first parent must equal the PR's current target branch and its second parent must equal `refs/pull/N/head`. That head must still be the head of a branch in the organization repository. A fork can therefore enter only as the triggering PR after existing membership admission, not indirectly as a companion on a privileged runner. Every companion must run Integration E2E in its own Ready state and obtain its own exact-head status: the primary PR's status cannot replace another PR's gate. Component wrappers do not subscribe to PR-body edits, so a marker change requires pushing a new head or a draft-to-ready transition. Before merging, compare the linked run's `source-set.tsv` against the current marker item by item. A marker changed after that run invalidates its evidence even if the commit status still displays success.

Admission and finalization use short-lived App tokens restricted to the six repositories with `Contents: read` and `Pull requests: read`. PR refs, target branch refs, commit parents and `branches-where-head` resolve each companion into PR number, candidate/base/base-ref/head SHA. The execution job does not trust raw PR-body text; it receives the resolved admission records, re-queries all refs/commits before executing candidate code, assembles sources by exact integration SHA and revokes the token. Finalization queries every record a third time. A missing merge ref, changed target branch/head/integration SHA or head that no longer belongs to an organization-repository branch makes the triggering PR's exact-head status fail.

The status is written only to the triggering PR's integration commit. Cross-repository changes must declare their companions reciprocally and each run Integration E2E, covering each owning component's E2E entry and obtaining separate statuses. After merging the first PR, remove its companion marker from subsequent PRs and rerun against the new sibling revision on the target version line. This freezes the source-validation set without adding product-version negotiation, compatibility aliases or temporary runtime gates. Source mode still has no `main` push or manual-dispatch entry.

Reusable-workflow validation requires:

1. The control plane and execution implementation both resolve from the trusted platform workflow revision (project `main`, resolved to its full SHA), with the full SHA recorded during the run.
2. The `pull_request_target` event, current PR and candidate/base/head inputs agree exactly.
3. The candidate and every companion have their respective base/head as their two integration-commit parents.
4. The current open PR and all companion refs retain the same base/head/merge commits before execution and after completion.
5. The fork-admission token requests only organization `Members: read` and exists only in the admission control job.
6. The E2E job that executes candidate code has a `GITHUB_TOKEN` with only `Contents: read` and `Pull requests: read`.
7. Companion/source App tokens have only six-repository `Contents: read` and `Pull requests: read`; runner-side tokens are revoked before candidate execution.

The final control job re-queries the PR. It sets the same `kuasar/ci-exact-head` status to `success` only when Integration E2E succeeded and the current integration commit still exactly matches admission. Test failure, cancellation, skips and a changed PR cannot produce successful merge evidence. GitHub's ordinary `pull_request_target` workflow check attaches to the PR head commit and cannot replace this integration-commit status.

Platform tooling and source are retrieved through GitHub's official archive API at full SHAs with read-only App tokens, without depending on Git smart HTTP. When their SHAs match, the already-extracted trusted tree is reused. Archives must have exactly one top-level directory and no path traversal, symbolic links or other nonregular entries. The five component source archives use `$KUASAR_SOURCE_CACHE_ROOT/<repo>/<sha>.tar.gz`: a job-local directory on hosted, or `/var/cache/kuasar/sources` on persistent runners. Hits validate SHA-256 and tar structure. Misses download GitHub's official tarball, falling back to the official zipball and local conversion on failure. Per-repository `flock` serializes maintenance, retaining the 32 most recently used revisions. Tokens are explicitly revoked before candidate code runs.

For a platform PR targeting `main`, all six units resolve from component `main`. For a target of `release/vMAJOR.MINOR.x`, the execution job first reads `releases/daily-preview.yaml` from the PR integration, then applies the formal Daily rules independently to derive each component's `release/vX.Y.x`. If that maintenance branch is absent, the unit stays pinned to the manifest's exact tag. `release-units.tsv` records the six independent configured versions, requested refs and resolved SHAs. The source workspace still has one `guest-runtime` checkout, using the runtime unit's SHA. The independent vmlinux archive, checksum and runtime combination are verified against the manifest by exact-assets release asset validation before formal publication. A single source checkout does not combine these two release units.

The job then creates a five-component `go.work`, restores or builds native cache, and validates and builds the candidate project sources:

```bash
make -C src/platform test-ci-tools test-release-tools test-perf-tools
make -C src/platform build e2e-tools assemble-e2e test-uffd-performance-gate
```

Each component maintains cases and a single `run_all.sh` entry in its own `test/e2e/`. Platform Integration E2E assembles the five component sources and the genuinely cross-component cases in `kuasar-sandbox/test/e2e/platform/` into:

```text
test/e2e/run_all.sh
test/e2e/accelerator/run_all.sh
test/e2e/connector/run_all.sh
test/e2e/guest-runtime/run_all.sh
test/e2e/sandboxer/run_all.sh
test/e2e/orchestrator/run_all.sh
test/e2e/platform/run_all.sh
```

A project-repository PR runs the top-level entry, which invokes all six owner entries in order. A component PR runs that component's complete assembled owner entry. When a component is the candidate repository, its assembled directory comes directly from that PR integration's `test/e2e/`. Feature implementation and its E2E are therefore reviewed together, and the component PR's full Integration E2E result depends on that component's `run_all.sh` passing in the unified environment. Needing another repository's binaries does not change test ownership: platform supplies the complete `BIN`, zot, versitygw, KVM and system-service environment without copying ownership of test sources.

Full source Integration E2E also runs one working-set smoke round covering A/B/C/D and local encryption `off/auto × cold/warm`. Each smoke run creates and exclusively owns a new TAP instead of reusing the preceding E2E's default interface. A run-specific `/32` host route isolates residual connected routes on the same subnet. Readiness-failure diagnostics are uploaded with CI metadata. Thirty-round canonical measurements generate stable descriptive performance reports; they are neither a separate workflow mode nor a mandatory PR round count.

## 3. Exact-assets mode

The aggregate workflow loads release tooling from trusted `main`, but prepare checks out the complete HEAD SHA of platform `main` or `release/vMAJOR.MINOR.x` explicitly selected by the dispatcher. That job uploads a short-lived Actions artifact containing:

```text
assets/             platform package, six component archives, unified SHA256SUMS
selection.tsv       workflow-internal version selection, not uploaded to GitHub Release
release-notes.md    GitHub Release page description
```

Integration E2E revalidates the fixed asset set, SHA-256, platform-package scope, safe tar paths and cross-package overwrites, then extracts into `release-install/`. Component archives supply runtime artifacts. Aggregate prepare has already collected component documentation and E2E into the platform package from GitHub source archives at the manifest-selected tags. The execution entry comes from that platform archive:

```bash
cd release-install
BIN=$PWD/bin bash test/e2e/run_all.sh
```

This mode neither infers nor reads component `main`. Each unit's documentation, cases and binaries bind to its independently selected tag, allowing a platform maintenance branch to combine different component maintenance lines. It does not rebuild product Go/Rust/native binaries, vmlinux, RocksDB or Cloud Hypervisor. On a Public caller, the trusted `exact-assets` bootstrap supplies host prerequisites; after bundle validation/extraction, [exact-assets-tools.sh](../ci/hosted/exact-assets-tools.sh) downloads zot v2.1.17 and builds the local versitygw v1.5.0 test service. Only host test tools and EROFS writer/readers are built. Product binaries in `release-install/bin` remain untouched. All ten existing `REQUIRE_*` flags stay enabled. The packaged top-level suite is partitioned into five isolated exact-assets shards (`core`, `sandboxer-main`, `sandboxer-defaults`, `orchestrator-a`, `orchestrator-b`); together they run every owner and every required case against the same validated package. After success, publish uses the earlier artifact unchanged.

## 4. Native cache

`ci/native-cache/native-cache.sh restore-or-build` handles:

- guest `vmlinux`;
- `mkfs.erofs` and `fsck.erofs`;
- guest `envd`;
- RocksDB headers and `librocksdb.a`;
- patched `cloud-hypervisor`.

Cache entries live at `$KUASAR_NATIVE_CACHE_ROOT/v2/<arch>/<component>/<input-hash>/`. Hosted bootstrap sets this root inside the disposable job directory; persistent runners retain `/var/cache/kuasar/native`. Hosted caches are local reuse only and are not uploaded to Actions cache or artifacts. The input hash covers build scripts, patches/configuration, upstream digests, architecture, Go/Cargo/C/C++ toolchains and pkg-config resolution. Entries are published through staging, checksums and atomic rename. Descriptor, payload and tar paths are checked again before restoring a hit. Corrupt entries fail rather than being repaired in place.

EROFS keys include Libgcrypt/Libgpg-error/uuid pkg-config metadata, target compiler/tool bytes, actual local source archive bytes (or the expected digest for a pinned URL), and a bounded compiler/static-link probe. The probe tracks consumed headers, including forced includes, and the archives/startup objects actually selected through flags, sysroots and library search paths. A source URL or filename is a locator, not content identity. Logical workspace file labels are relocatable; meaningful compiler and sysroot flag values remain significant. An unpinned URL cannot authorize a shared cache; use a pinned URL or a local archive.

Optional `guest-runtime/native-deps/deps/erofs-patches` material, ordered `series` and `deps/erofs-recipe.sh` enter the key. Older source sets without those files are supported, including the previous OpenSSL recipe's actual target link probe. Adding, changing or removing inputs invalidates the key. Hosted native profiles install `libgcrypt20-dev libgpg-error-dev uuid-dev` and retain `libssl-dev` for already-admitted older source sets. openEuler 24.03-LTS-SP4's `libgcrypt-1.10.2-4` and `libgpg-error-1.47-1` source RPMs explicitly disable static libraries; their devel packages alone are insufficient. The [runner provider](../ci/runner/README.md#install) builds those pinned distro-patched sources with at most two jobs, installs only the static archives and validated source/build/relink/license catalogs, and verifies warm reuse and template-to-slot copies. Runtime packaging validates the same pinned catalogs; Ubuntu keeps the installed-package material path.

EROFS retains one optional `bin/<arch>/.erofs-recipe` v2 stamp with both output hashes and actual external compiler/link dependencies, both link maps and their EROFS object/archive inputs, and source `LICENSES`, `AUTHORS` and `COPYING`. Restoring an identical recipe can reuse the outputs without an extracted source tree. Older caches without the stamp remain readable and rebuild on the next recipe check. Repository patch files remain from the admitted source set; cache restore does not replace them. Runtime patch-material validation uses the selected commit's local Git objects; those objects must be available to standalone validators. Actual target copyright/notices and source/relink inputs remain required for real release packaging.

Build and restore of the same key hold an entry lock. Each component retains its four most recently used keys by default. Reclamation only removes entries beyond the protection period whose locks can be acquired without blocking. Cache tests:

```bash
make -C kuasar-sandbox test-ci-tools
```

## 5. Runners and networking

### 5.1 Visibility-only standard runner routing

The main migration is tracked in [#127](https://github.com/kuasar-sandbox/kuasar-sandbox/issues/127).
The shared control and E2E jobs select `ubuntu-latest` exactly when the actual
calling repository is Public. Every other visibility retains the existing
`kuasar-control` or `kuasar-e2e` pool. Repository names, candidate identity and
mode do not choose the runner; invalid inputs fail the independent request
validator on the selected runner. The reusable workflow repository and fork
visibility are not the calling repository's visibility.

Control bootstrap uses the same Public condition. Mode still chooses the
`source` or `exact-assets` profile, credentials, workload and timeout: exact
assets keep 120 minutes; other hosted work keeps 180 and private source keeps
60. No migration allowlist, extra runner decision job or paid fallback exists.
Host selection follows `ubuntu-latest`, but toolchain, dependency and auxiliary
service versions and hashes remain pinned. Capability checks require Ubuntu
Linux, x86_64 and the expected hosted environment, not a numbered image.
Removing that version gate does not qualify an untested future Ubuntu image.

Each job chooses the profile needed for its actual work:

| Profile | Jobs / prerequisites |
| --- | --- |
| `control` | PR admission/finalization, release cleanup/reconcile; Git, curl, jq, Python/YAML and archive tools |
| `release-control` | Release preflight, Kernel publish and Preview deletion; control tools plus Go |
| `kernel` | Kernel build; Go and Ubuntu Kbuild development packages |
| `runtime` | Runtime build; Go, native development packages and trusted EROFS writer/readers |
| `runtime-publish` | Runtime publish validation; Go and separately built EROFS writer/readers |
| `source` | Complete source build/E2E; all native prerequisites, Rust/Docker checks, VM/network tools |
| `exact-assets` | Packaged binaries/tests; Docker, systemd/cgroup v2, KVM/UFFD/netns/BPF, runtime utilities, EROFS host tools; pinned Go only for versitygw |

Go comes from the official `go1.26.5.linux-amd64.tar.gz`, SHA256
`5c2c3b16caefa1d968a94c1daca04a7ca301a496d9b086e17ad77bb81393f053`.
The bootstrap verifies the archive, driver and compiler before adding it to
PATH; source-module toolchain selection retains `GOTOOLCHAIN=auto`. EROFS host writer/readers
use pinned v1.9.1 source and its SHA256, independently of the static guest
recipe. Docker, and Rust/Cargo for source builds, are required capabilities of the
[standard image](https://github.com/actions/runner-images#available-images)
and are checked explicitly. Native source pins, Cargo lockfiles, build flags,
link maps and materials remain owned by their existing recipes. Missing tools,
checksum mismatches or unavailable VM capabilities fail the job.

The source and exact-assets profiles load `tun` and `vhost_vsock`, enable
`vm.unprivileged_userfaultfd=1`, and check userfaultfd, systemd and cgroup v2.
A headless runner's udev `uaccess` processing can remove the named runner ACL
from `/dev/kvm` without replacing the device. Before host changes, VM bootstrap
requires the expected GitHub-hosted Ubuntu Linux x64 environment and validates
non-root numeric job uid/primary gid. It installs only
`/etc/udev/rules.d/99-kuasar-job-kvm.rules`, matching `SUBSYSTEM=="misc"` and
`KERNEL=="kvm"`, with final `GROUP:="<id -g>"` and `MODE:="0660"` assignments.
It reloads rules, triggers only KVM, waits for udev to settle, and applies the
same group/mode only to `/dev/kvm`. It then removes the named runner KVM ACL to
replay the observed loss and verifies `O_RDWR` as the current job user.
Group membership is unchanged; no world access is granted. Vhost-vsock and
TUN retain their original per-job ACLs. The suite and its pre-sudo access checks
remain unchanged; full hosted qualification is still required.

The required packaged connector E2E verifies actual namespaces and BPF data
paths; bootstrap does not require an additional kernel-version-specific probe. Both profiles explicitly install full-suite utilities such as ping,
netcat, OpenSSL, SQLite and `strace` for the sandboxer usage fault/source
fixtures. Neither installs a runner service, template, nspawn slot or persistent
network.
The old owned-state recovery runs only on persistent runners. Builds use CPU
affinity bounded by available CPUs and memory (2 GiB OS reserve, 2 GiB per
compiler job), including recipes using `nproc`; Go/Cargo parallelism uses the
same budget. Top-level Make goals retain their sequential order. Source E2E has
the existing 180-minute hosted budget for a cold build and the complete
owner/UFFD/A/B/C/D smoke sequence; exact-assets retains 120 minutes and private
source retains 60. Test assertions and the full working-set matrix are unchanged.

Source archives, native entries, tarballs, Go/Cargo caches and tools live under
`$RUNNER_TEMP/kuasar-hosted.*`; exact-assets test tools use a separate
`$RUNNER_TEMP/kuasar-exact-tools.*` tree. They are never uploaded. Sources themselves stay
in the job workspace; only the existing revision/timing/performance metadata
and validated release bundles are uploaded. Release bundles retain their
required license/source inventories, without a full workspace handoff. Hosted
uses official Go/Rust/Python/kernel/image endpoints. Source mode retains its
existing zot/versitygw targets. Exact-assets reuses trusted platform
`ensure-zot.sh` and only the public guest-runtime `build-versitygw.sh`/`common.sh`
from [commit 494dbceae683d6b20cdbec00fe6b1f554ea2f508](https://github.com/kuasar-sandbox/guest-runtime/tree/494dbceae683d6b20cdbec00fe6b1f554ea2f508/native-deps/deps).
Fixed recipe paths, commit and SHA256 pins are checked; versitygw source also
passes archive path/type validation. Go is pinned for this host tool build, with
bounded affinity/parallelism and local caches. `host-tools.tsv` records recipe,
source and binary identities. No private sibling checkout or candidate-selected
host build script is used in exact-assets. Persistent callers keep their mirror
settings.

Integration bootstrap runs from `trusted/platform` at `job.workflow_sha`, after
request validation and platform tooling-token revocation. The validated mode
selects `source` or `exact-assets`; only source mode creates a source token and
attaches source caches. Exact-assets attaches its tools only after downloading,
validating and extracting the bundle. Guest release jobs
check out that same immutable platform pin before requested sources; Runtime
ABI checks come from the trusted guest workflow checkout. Kernel needs no
cross-repository App token. Runtime still fetches its private dependency closure
with the existing read-only source token and revokes it before executing source
or packaging code. Publishing remains in a separate job with write permission.

Rollout follows the corrected main-first, public-before-hosted order: finish
main's own source/aggregate qualification first, then prepare and review each
remaining private repository, publish that repository, and verify its own
standard-runner CI. Accelerator #129/#135 remains deferred as component-specific work. Sequential
preparation/publication/validation is a work order, not a shared routing allowlist.
A later Public transition selects hosted automatically; component-owned release
workflows still require their own preparation and real acceptance. No public relay
for a private component is introduced.
Main's genuine aggregate/source-set tests may use the existing authorized
companion dependencies, but do not qualify component publication. The completed
guest route and immutable release bootstrap pins are retained; future pin changes
must use the reviewed upstream SHA. PR wrappers keep `@main`, and
`pull_request_target` executes the base-branch wrapper. Candidate-only edits or
manual rehearsals cannot replace the required exact-candidate Integration E2E
check. The local authoring Git baseline is not upstream release provenance.

Standard-runner functional results do not prove the previous fixed-hardware VM
capacity. Optional real-cloud OBS/NFS tests still need their own environment and
evidence; local zot/versitygw fixtures do not establish real-cloud acceptance.
Required capability or resource failures must fail, not become successful skips.

Offline checks are `python3 ci/hosted/test-workflows.py` (also included in
`make test-ci-tools`) and guest `python3 scripts/ci-test-workflows.py
../kuasar-sandbox`. They cover runner selection, profiles, pins, token ordering,
private cache paths, ABI and required coverage. Syntax/offline success is not
hosted qualification: the supervisor must exercise the actual candidate run,
including the full working-set matrix and truthful revision metadata, after
activating the trusted rollout.

### 5.2 Remaining persistent callers

Runner installation material lives in `ci/runner/`. Unmigrated component release-control jobs use the dedicated `kuasar-control` pool. For non-public callers, shared candidate-executing E2E jobs use the `kuasar-e2e` pool and restricted read tokens. These runner classes have different root filesystems, work directories, labels and GitHub runner groups. Changing roles requires cleaning and rebuilding from a trusted template, not relabeling in place. `kuasar-control` is visible to all organization repositories, including public repositories, but its workflow allowlist permits only central `ci-entry.yml` and release workflows on component `main`. Public-repository CI admission/finalization, Daily coordination and aggregate-release control jobs use GitHub-hosted runners. Runner proxies are deployment configuration and are not embedded in repository workflows. Public mainland-China mirrors for Go, Rust, Python, the Linux kernel and common container images reduce network variability.

At persistent-runner job start, reset stops leftover sandbox systemd units, removes test TAPs and reloads systemd. It also reclaims the runner's working-set network namespace using the deterministic `kuasar-ws-<hash8>` name derived from `RUNNER_NAME`: send TERM to its processes, send KILL after a bounded wait, confirm no process is alive, then delete the namespace. Name derivation prevents runners on the same host from affecting one another and lets the next job reclaim a SIGKILL-interrupted run by exact name without guessing interface ownership (#43, #53). Common zot and versitygw tools are linked from the runner's fixed tool directory into the current workspace; they do not enter release packages. The `kuasar-e2e` runner group retains `visibility=all` for organization repositories, has no workflow allowlist and runs only candidate E2E. Fork-workflow secret forwarding is disabled in every repository. Preparation steps needing App secrets exist only in trusted base-repository workflows, and their tokens are revoked before candidate code executes.

Privileged Runner provisioning must keep publishing credentials and persistent Runner credentials outside candidate access, and discard candidate-writable state between jobs. Revoking source tokens alone does not establish that isolation. Unreviewed outside-fork code must not enter these privileged slots; maintainers first review and accept its exact source through the documented contribution flow.

## 6. Run artifacts

Every Integration E2E run uploads `ci-metadata-<run>-<attempt>`. Source mode normally includes:

- `run.tsv`: mode, candidate repository, PR and candidate/base/base-ref/head SHA;
- `source-set.tsv`: triggering candidate and all companion PRs, candidate/base/base-ref/head SHA and roles;
- `revisions.tsv`: exact revisions of platform test tooling and the five components;
- `release-units.tsv`: configured versions, derived refs and resolved SHAs of the six Daily release units;
- `source-cache.tsv`: source-cache hits and digests;
- `native-cache.tsv`: native keys, hits and durations;
- `timings.tsv`: build/test stage resource data;
- UFFD and working-set reports, only when those tests ran.

Exact-assets mode records the run, test output and hosted `host-tools.tsv` identities without inventing component-source or native-cache metadata. The release combination is expressed by the manifest at the exact selected platform-branch commit, component tags and GitHub release notes. It need not come from platform `main`: maintenance-branch aggregates retain their own selection.

## 7. See also

- [release.md](release.md): release assets, release asset validation and permission boundaries;
- [deployment.md](deployment.md): system services and runtime environment required by Integration E2E;
- [../ci/runner/README.md](../ci/runner/README.md): runner installation and maintenance;
- [../test/QUICKSTART.md](../test/QUICKSTART.md): E2E prerequisites and troubleshooting.
