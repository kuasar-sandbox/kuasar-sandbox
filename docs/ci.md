[English](ci.md) | [简体中文](ci_zh.md)

# Continuous integration

<a id="1-概述"></a>
## 1. Overview

The project repository owns the trusted CI control plane and the shared execution workflows. The five component repositories retain only event triggers and parameter wrappers, referencing `.github/workflows/ci-entry.yml` from project `main`. That entry calls `.github/workflows/integration-tests.yml` from the same resolved project `main` revision. Common admission, runner initialization, source caching, native caching, full E2E and exact released-asset validation are not duplicated across repositories.

The execution workflow has two explicit modes:

- `source`: validates the triggering repository's PR integration commit, an optional exact companion-PR set and the other sources resolved for the target platform branch;
- `exact-assets`: validates release archives already downloaded and verified by the aggregate workflow, without rebuilding them.

<a id="2-source-模式"></a>
## 2. Source mode

Each repository's trusted wrapper receives `pull_request_target` events without executing workflows supplied by the candidate repository. It accepts only PRs targeting `main` or `release/vMAJOR.MINOR.x`. Non-draft same-repository PRs are admitted automatically. For fork PRs, a trusted control job queries current organization membership using a read-only App token; only an `active` owner/member is admitted automatically. Public-repository admission and finalization use GitHub-hosted runners. The control job also re-queries the current PR, validates GitHub's generated two-parent integration commit and sets `kuasar/ci-exact-head` to `pending` on that commit. Event `author_association` is not the source of organization membership. Draft PRs run only admission/finalize control steps, not full E2E, and retain a `pending` exact-head status. Marking a draft ready causes a new event to run full Integration E2E. External forks, conflicts and events whose inputs have changed are rejected.

The other repositories' revisions are resolved through the GitHub REST API from the refs selected for the target platform branch. The six release units are resolved independently, including runtime and vmlinux; the source workspace uses the runtime unit for its single `guest-runtime` checkout, as detailed below. The candidate repository's revision is replaced with its admitted integration commit. An ordinary PR declares no companions and retains single-candidate behavior. Atomic cross-repository changes that cannot build against the other repositories' `main` can declare one another in both PR bodies:

```text
<!-- kuasar-ci-companions
kuasar-sandbox/orchestrator#227
-->
```

The marker may occur at most once. Each nonempty line must be an allowed `owner/repository#PR`. A repository may appear only once, and a PR cannot reference its triggering repository. Empty, duplicate and unclosed blocks are rejected. A companion must retain `refs/pull/N/merge`; the integration commit's first parent must equal the PR's current target branch and its second parent must equal `refs/pull/N/head`. That head must still be the head of a branch in the organization repository. A fork can therefore enter only as the triggering PR after existing membership admission, not indirectly as a companion on a privileged runner. Every companion must run Integration E2E in its own Ready state and obtain its own exact-head status: the primary PR's status cannot replace another PR's gate. Component wrappers do not subscribe to PR-body edits, so a marker change requires pushing a new head or a draft-to-ready transition. Before merging, compare the linked run's `source-set.tsv` against the current marker item by item. A marker changed after that run invalidates its evidence even if the commit status still displays success.

Admission and finalization use short-lived App tokens restricted to the six repositories with `Contents: read` and `Pull requests: read`. PR refs, target branch refs, commit parents and `branches-where-head` resolve each companion into PR number, candidate/base/base-ref/head SHA. The self-hosted job does not trust raw PR-body text; it receives the resolved admission records, re-queries all refs/commits before executing candidate code, assembles sources by exact integration SHA and revokes the token. Finalization queries every record a third time. A missing merge ref, changed target branch/head/integration SHA or head that no longer belongs to an organization-repository branch makes the triggering PR's exact-head status fail.

The status is written only to the triggering PR's integration commit. Cross-repository changes must declare their companions reciprocally and each run Integration E2E, covering each owning component's E2E entry and obtaining separate statuses. After merging the first PR, remove its companion marker from subsequent PRs and rerun against the new sibling revision on the target version line. This freezes the source-validation set without adding product-version negotiation, compatibility aliases or temporary runtime gates. Source mode still has no `main` push or manual-dispatch entry.

Reusable-workflow validation requires:

1. The control plane and execution implementation both resolve from project `main`, with the resolved full SHA recorded during the run.
2. The `pull_request_target` event, current PR and candidate/base/head inputs agree exactly.
3. The candidate and every companion have their respective base/head as their two integration-commit parents.
4. The current open PR and all companion refs retain the same base/head/merge commits before execution and after completion.
5. The fork-admission token requests only organization `Members: read` and exists only in the admission control job.
6. The E2E job that executes candidate code has a `GITHUB_TOKEN` with only `Contents: read` and `Pull requests: read`.
7. Companion/source App tokens have only six-repository `Contents: read` and `Pull requests: read`; runner-side tokens are revoked before candidate execution.

The final control job re-queries the PR. It sets the same `kuasar/ci-exact-head` status to `success` only when Integration E2E succeeded and the current integration commit still exactly matches admission. Test failure, cancellation, skips and a changed PR cannot produce successful merge evidence. GitHub's ordinary `pull_request_target` workflow check attaches to the PR head commit and cannot replace this integration-commit status.

Platform tooling and source are retrieved through GitHub's official archive API at full SHAs with read-only App tokens, without depending on Git smart HTTP. When their SHAs match, the already-extracted trusted tree is reused. Archives must have exactly one top-level directory and no path traversal, symbolic links or other nonregular entries. The five component source archives are cached at `/var/cache/kuasar/sources/<repo>/<sha>.tar.gz`. Hits validate SHA-256 and tar structure. Misses download GitHub's official tarball, falling back to the official zipball and local conversion on failure. Per-repository `flock` serializes maintenance, retaining the 32 most recently used revisions. Tokens are explicitly revoked before candidate code runs.

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

<a id="3-exact-assets-模式"></a>
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

This mode neither infers nor reads component `main`. Each unit's documentation, cases and binaries bind to its independently selected tag, allowing a platform maintenance branch to combine different component maintenance lines. It does not invoke Go/Rust/native builds or recompile vmlinux. After success, publish uses the earlier artifact unchanged.

## 4. Native cache

`ci/native-cache/native-cache.sh restore-or-build` handles:

- guest `vmlinux`;
- `mkfs.erofs` and `fsck.erofs`;
- guest `envd`;
- RocksDB headers and `librocksdb.a`;
- patched `cloud-hypervisor`.

Cache entries live at `/var/cache/kuasar/native/v2/<arch>/<component>/<input-hash>/`. The input hash covers build scripts, patches/configuration, upstream digests, architecture, Go/Cargo/C/C++ toolchains and pkg-config resolution. Entries are published through staging, checksums and atomic rename. Descriptor, payload and tar paths are checked again before restoring a hit. Corrupt entries fail rather than being repaired in place.

Build and restore of the same key hold an entry lock. Each component retains its four most recently used keys by default. Reclamation only removes entries beyond the protection period whose locks can be acquired without blocking. Cache tests:

```bash
make -C kuasar-sandbox test-ci-tools
```

<a id="5-runner-与网络"></a>
## 5. Runners and networking

Runner installation material lives in `ci/runner/`. Release-control jobs use the dedicated `kuasar-control` pool. Candidate-executing E2E jobs continue to use the `kuasar-e2e` pool and restricted read tokens. These runner classes have different root filesystems, work directories, labels and GitHub runner groups. Changing roles requires cleaning and rebuilding from a trusted template, not relabeling in place. `kuasar-control` is visible to all organization repositories, including public repositories, but its workflow allowlist permits only central `ci-entry.yml` and release workflows on component `main`. Public-repository CI admission/finalization, Daily coordination and aggregate-release control jobs use GitHub-hosted runners. Runner proxies are deployment configuration and are not embedded in repository workflows. Public mainland-China mirrors for Go, Rust, Python, the Linux kernel and common container images reduce network variability.

At job start, reset stops leftover sandbox systemd units, removes test TAPs and reloads systemd. It also reclaims the runner's working-set network namespace using the deterministic `kuasar-ws-<hash8>` name derived from `RUNNER_NAME`: send TERM to its processes, send KILL after a bounded wait, confirm no process is alive, then delete the namespace. Name derivation prevents runners on the same host from affecting one another and lets the next job reclaim a SIGKILL-interrupted run by exact name without guessing interface ownership (#43, #53). Common zot and versitygw tools are linked from the runner's fixed tool directory into the current workspace; they do not enter release packages. The `kuasar-e2e` runner group retains `visibility=all` for organization repositories, has no workflow allowlist and runs only candidate E2E. Fork-workflow secret forwarding is disabled in every repository. Preparation steps needing App secrets exist only in trusted base-repository workflows, and their tokens are revoked before candidate code executes.

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

Exact-assets mode records the run and test output without inventing source or native-cache metadata. The release combination is expressed by the manifest at the exact selected platform-branch commit, component tags and GitHub release notes. It need not come from platform `main`: maintenance-branch aggregates retain their own selection.

## 7. See also

- [release.md](release.md): release assets, release asset validation and permission boundaries;
- [deployment.md](deployment.md): system services and runtime environment required by Integration E2E;
- [../ci/runner/README.md](../ci/runner/README.md): runner installation and maintenance;
- [../test/QUICKSTART.md](../test/QUICKSTART.md): E2E prerequisites and troubleshooting.
