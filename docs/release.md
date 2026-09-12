[English](release.md) | [简体中文](release_zh.md)

# Release

<a id="1-概述"></a>
## 1. Overview

Kuasar Sandbox separates component publication from platform aggregate publication. A component version describes an independent component delivery; an aggregate version describes a set of already-published components that passed exact-asset validation together. Neither version is derived from the other, and they need not have matching names.

The release units are:

- `accelerator`, `connector`, `sandboxer`, `orchestrator`: `vX.Y.Z`;
- `guest-runtime` runtime: `runtime-vX.Y.Z`;
- `guest-runtime` kernel: `vmlinux-vX.Y.Z`;
- platform aggregate: `release-vX.Y.Z`.

`guest-runtime` has runtime and vmlinux release units but remains one component repository. Every version line accepts the `-preview.YYYYMMDD` suffix. Preview is a GitHub prerelease; Stable is not a prerelease. Published versions are never overwritten, renamed or rebuilt.

<a id="2-配置规约"></a>
## 2. Configuration contract

Each maintained platform branch uses exactly two manifests:

- `releases/release.yaml`: the next planned Stable aggregate version on that branch;
- `releases/daily-preview.yaml`: the branch's currently maintained Daily Preview version.

This is the release-state contract, not a second version-metadata system or historical directory. Earlier selections remain in each manifest's first-parent Git history. The parser always requires:

```text
daily-preview.yaml/version >= release.yaml/version
```

The comparison uses numeric aggregate `MAJOR.MINOR.PATCH` values, not string ordering. A Stable manifest can select only Stable components. A Preview manifest can mix Stable and Preview components. Both manifests must list exactly six release units with their respective correct tag prefixes.

The two manifests below are examples, not a list of current published versions.

<a id="21-stable-清单"></a>
### 2.1 Stable manifest

`previous_version` is optional for the first Stable release. When present it names an earlier Stable release; release notes never substitute a Preview or the repository's entire history as the first Stable baseline.

```yaml
version: release-v0.5.7
previous_version: release-v0.5.6
components:
  accelerator: v0.2.1
  connector: v0.1.9
  sandboxer: v0.3.5
  orchestrator: v0.4.3
  runtime: runtime-v0.1.2
  vmlinux: vmlinux-v0.1.0
```

`previous_version` must be strictly earlier than `version`. A formal aggregate can select component versions that differ from one another and from the aggregate version.

<a id="22-daily-preview-清单"></a>
### 2.2 Daily Preview manifest

```yaml
version: release-v0.5.7
previous_version: release-v0.5.6
preview_version: preview.20260831
previous_preview_version: preview.20260830
components:
  accelerator: v0.2.2-preview.20260831
  connector: v0.1.9
  sandboxer: v0.3.6-preview.20260831
  orchestrator: v0.4.3
  runtime: runtime-v0.1.2
  vmlinux: vmlinux-v0.1.0
```

The complete aggregate tag is `version + "-" + preview_version`. Later Previews on the same aggregate line use `previous_preview_version` as their update baseline. The first Preview on that line uses `previous_version`.

Advance `previous_preview_version` only after the current aggregate has a complete published Release. When an unpublished selection rolls over to a new date, retain its existing baseline (or keep the field absent for the first Preview). An abandoned selection may contain component tags that were never published and cannot serve as an update baseline.

After Stable `V1` is published, continued development on the same branch must first advance Daily to `V2` and set `previous_version: V1`. Advance the Stable manifest when preparing `V2`. After `V2`, advance both toward `V3`, and so on. When Daily and Stable name the same version and that Stable aggregate has already been published, the line is closed: no new Preview and no repeated Stable publication. If the same-version Stable Release exists but its assets or tag are incomplete, Daily also defers before any manifest or component changes; it does not publish a Preview alongside an incomplete Stable.

<a id="3-分支与版本线"></a>
## 3. Branches and version lines

Platform `main` publishes the latest mainline version. Maintenance patches use platform `release/vMAJOR.MINOR.x`. For example, after publishing `release-v0.1.0`, a `release/v0.1.x` branch can be created from that tag. Maintenance then publishes `release-v0.1.1` and `release-v0.1.2`, while mainline can later publish `release-v0.2.0`.

If `release-v0.2.0` is not yet planned and `release-v0.1.0` immediately needs patches, `main` can first publish `release-v0.1.1` and `release-v0.1.2`, then create `release/v0.1.x` from a later patch point. Once that branch exists, mainline and maintenance each manage their own two manifests.

Platform GitHub Latest is updated only by a Stable aggregate published from platform `main`. Each component repository independently manages its own Latest. A Stable publication from component `main` records its source branch and exact commit. A separate idempotent reconciliation workflow selects Latest from all published mainline Stable releases by source-commit order, comparing SemVer only when multiple tags refer to the same commit. Any completed component publication triggers reconciliation. It is serialized across versions, can be rerun independently after failure and has scheduled self-healing. Component maintenance Stable releases and all Previews do not update Latest. Platform aggregates always select components by exact manifest tags, not by component Latest.

Platform and component maintenance branches need not have matching names. Platform `release/v0.5.x` can aggregate `sandboxer release/v0.3.x`, `orchestrator release/v0.4.x` and a fixed vmlinux version released only from `main`.

<a id="4-daily-分支扫描与组件选择"></a>
## 4. Daily branch scanning and component selection

`Daily Preview Scanner` runs daily on an Asia/Shanghai schedule, scanning:

- platform `main`;
- every platform branch matching `release/vMAJOR.MINOR.x`.

The scanner pins each branch HEAD SHA, then loads the trusted controller from platform `main` to process that SHA. The controller accesses component repositories with a read-only contents GitHub App token. The token is passed only through a subprocess-scoped Git HTTP authorization header, never through remote URLs, repository configuration or logs. All branch coordinators and Preview GC share one GitHub Actions concurrency key, preventing manifest selection from overlapping GC planning/dispatch. The scanner dispatches and waits for branches in order. Cancelled or timed-out branch tasks receive at most three attempts in that scan; later scans recover work that remains incomplete. If a branch moves during selection or manifest commit, the current run does not overwrite remote state; the next run starts from the new HEAD. A deterministic failure is recorded while other branches continue, and the scanner reports failure at the end. One broken maintenance line therefore cannot indefinitely starve other branches.

<a id="41-源分支映射"></a>
### 4.1 Source-branch mapping

- Platform `main` always selects every component repository's `main`.
- A platform maintenance branch derives the component branch from that unit's Daily-manifest version. Both `v0.3.5` and `v0.3.6-preview.*` map to component `release/v0.3.x`.
- Runtime and vmlinux independently derive `release/vX.Y.x` from `runtime-vX.Y.Z` and `vmlinux-vX.Y.Z`.
- If the derived branch is absent, that unit reuses the manifest's complete Release without automatically changing its version. If an upstream dependency changes in the current round and the unit needs rebuilding, the entire selection is deferred. It does not write an aggregate manifest combining the old downstream binary with new dependency versions.

The six units in one platform maintenance branch can therefore come from six different component version lines.

<a id="42-tag-选择"></a>
### 4.2 Tag selection

Tag selection does not choose the largest SemVer across all branch history. It walks backward from the selected branch HEAD along first-parent history. The first commit with a complete usable Release tag wins. SemVer selects the largest valid tag only among tags on that same commit. A maintenance branch also ignores tags outside its `MAJOR.MINOR` line.

The Daily manifest's configured tag participates in the same commit-position comparison:

- the commit closer to HEAD wins;
- on the same commit, the largest SemVer wins;
- a configured tag outside the selected branch's first-parent history stops selection, preventing accidental selection of a side branch or historical major version.

A winning tag already at HEAD is reused. If a winning Stable tag is behind HEAD, Patch is incremented once to produce `vX.Y.(Z+1)-preview.YYYYMMDD`. If a winning Preview is behind HEAD, its core version is retained and only the date changes. The next scan sees that Preview and does not repeatedly increment Patch.

Dependency changes also rebuild units that actually carry those dependencies. An accelerator change directly rebuilds sandboxer, orchestrator and runtime; a connector change directly rebuilds sandboxer and orchestrator; and a sandboxer change directly rebuilds orchestrator and runtime. Connector changes therefore also rebuild runtime transitively through the newly selected sandboxer. These changes do not rebuild vmlinux. If a unit already published a Preview that day and dependencies change again, the process defers until the next Preview date. It neither reuses assets built with old dependencies nor invents a same-day sequence-number format.

A code change on the selected platform branch also creates a new aggregate Preview even if all six components are reused. Workflow run names for an aggregate version bind its platform source SHA. While any matching run remains active, the controller leaves the current Daily manifest unchanged, even if a newer run was cancelled or finished. A new branch HEAD gets a new run identity; old inputs are not rerun and an in-flight manifest is not rewritten. A branch-coordination task's identity also includes its requested date, so scans on different dates at the same platform SHA cannot incorrectly reuse each other's results.

<a id="5-残缺发布恢复"></a>
## 5. Recovering incomplete publication

A complete component Release must be non-draft, have a prerelease state consistent with its tag, contain only the contracted archive and `SHA256SUMS`, and have both assets in uploaded state. A complete aggregate Release must meet the eight-asset contract.

Only tags with complete Releases are selection candidates. Incomplete states do not fail the entire Daily schedule:

- If a matching publication run is queued or in progress, defer and recover in a later scan.
- For an incomplete Preview owned by the current Daily manifest, first call the owning component repository's deletion workflow. Verify the exact source SHA, delete the Release, assets and tag, then rescan and follow ordinary Daily publication.
- Ignore foreign incomplete Previews and incomplete Stable releases; neither select nor automatically delete them.
- If an incomplete fixed version has no component maintenance branch, defer rather than derive a substitute version.
- Deterministic failures receive at most three attempts, then fail the branch coordinator and scanner. Recovery does not overwrite tags or manually assemble assets. Ordinary component-publication/deletion failures rerun only failed jobs. Cancelled, timed-out or similar states without failed jobs rerun the full workflow. Every unsuccessful aggregate publication instead creates a new workflow run with the same exact inputs, letting prepare fetch component Releases again and create a fresh exact stage. The three-attempt budget counts the sum of `run_attempt` across those runs.

When an unfinished aggregate selection crosses into a new Shanghai date, the controller no longer binds the old date's tags to new component HEADs. It first deletes incomplete aggregate objects through the protected entry, then advances the manifest date and reselects. Complete component Previews in the old selection that no retained aggregate references are left for GC after the rollback window. Within the same day, the controller stays deferred rather than overwriting already-public complete Preview tags.

Recovery restores the Daily process; it does not patch or rebuild assets of incomplete Releases. The `incomplete` mode never deletes a complete same-name Release. When a tag is already missing but its draft or prerelease remains, recovery deletion is allowed only if `target_commitish` is a full source SHA matching the expected source. If a same-name incomplete object reappears after successful cleanup, the controller starts a new cleanup run instead of treating historical success as proof of current convergence.

<a id="6-组件发布-cli"></a>
## 6. Component publication CLI

Component workflows always load trusted tooling from repository `main`, but require the actual source branch and its exact HEAD SHA as explicit inputs. The following shows parameter shapes only; automatic Daily fills these values:

```bash
gh workflow run release.yml \
  --repo kuasar-sandbox/accelerator --ref main \
  -f version=v0.2.2-preview.20260831 \
  -f source_ref=release/v0.2.x -f source_sha=<full-sha> \
  -f aggregate_version=release-v0.5.7-preview.20260831 \
  -f aggregate_sha=<platform-manifest-commit>

gh workflow run release.yml \
  --repo kuasar-sandbox/sandboxer --ref main \
  -f version=v0.3.6-preview.20260831 \
  -f source_ref=release/v0.3.x -f source_sha=<full-sha> \
  -f aggregate_version=release-v0.5.7-preview.20260831 \
  -f aggregate_sha=<platform-manifest-commit> \
  -f accelerator_version=v0.2.2-preview.20260831 \
  -f connector_version=v0.1.9
```

Orchestrator receives `accelerator_version`, `connector_version` and `sandboxer_version`; runtime receives only `accelerator_version` and `sandboxer_version`, matching the code embedded in its image; vmlinux has no internal component-version input. Runtime and vmlinux use their own workflows. Preflight requires `source_sha` still to be the HEAD of `source_ref`. Build checks out that SHA, and the final tag points to it. Dependency inputs must name exact, already-complete Releases.

Preview additionally requires that `daily-preview.yaml/version + preview_version` at the platform commit identified by `aggregate_sha` exactly matches the aggregate version, and that `components.<unit>` exactly matches the tag being published. This binding and the Stable-line-closure check run before build and again after acquiring the publication concurrency lock. Every component's release notes record source branch, source SHA and release unit. Preview notes also record the original aggregate-manifest SHA and actual dependency-version bindings. An existing Preview can be reused only when both source and dependency bindings match.

Workflow run names include source SHA and the dependency-version tuple. The controller reruns failed runs only for identical input tuples. Changed branch HEADs or dependency selections create a new dispatch rather than consuming the three recovery attempts with obsolete inputs.

<a id="7-聚合发布-cli"></a>
## 7. Aggregate publication CLI

To converge an aggregate version already committed in the selected branch, the local release entry first handles the required component units and then the aggregate transaction:

```bash
RELEASE_VERSION=$(awk '$1 == "version:" {print $2}' \
  kuasar-sandbox/releases/release.yaml)
PLATFORM_REF=$(git -C kuasar-sandbox branch --show-current)
PLATFORM_SHA=$(git -C kuasar-sandbox rev-parse HEAD)
make -C kuasar-sandbox release RELEASE_VERSION="$RELEASE_VERSION" \
  PLATFORM_REF="$PLATFORM_REF" PLATFORM_SHA="$PLATFORM_SHA"
```


The aggregate workflow also loads trusted tooling from platform `main`, but version selection, system documentation, platform cases and package content come from the exact HEAD of the selected platform branch. Old release scripts on that target branch are not executed as the controller:

```bash
gh workflow run aggregate-release.yml \
  --repo kuasar-sandbox/kuasar-sandbox --ref main \
  -f version=release-v0.5.7 \
  -f source_ref=release/v0.5.x \
  -f source_sha=<full-sha>
```

Before formal publication, commit `release.yaml` on the target branch, publish missing Stable component units, then trigger the aggregate from that branch's latest HEAD. A successful mainline Stable becomes Latest. A maintenance Stable remains a discoverable formal release without displacing mainline Latest. Both Stable and Preview must be directly selected by that HEAD's current manifest. Historical manifests are only for resolving existing Releases and GC; manual dispatch cannot backfill them as new aggregate publications.

The short-lived artifact produced by aggregate prepare is immutable publication evidence for that run. After aggregate failure, do not rerun failed jobs or the full old workflow. Redispatch the same version, source branch and source SHA so a new prepare downloads current component Releases. The controller totals attempts across old and new runs with the exact run name, reporting failure after three attempts.

Local release-tool validation:

```bash
make test-release-tools
make test-ci-tools
```

<a id="8-资产与-bms"></a>
<a id="8-assets-and-bms"></a>
## 8. Release asset validation

Each ordinary component Release contains exactly its component archive and `SHA256SUMS`. Runtime and vmlinux have independent archive names. An aggregate Release contains exactly a platform archive, six unchanged component archives and one unified `SHA256SUMS`: eight explicit assets.

Do not publish generated release-metadata JSON or duplicate GitHub's automatically supplied source archives. Selection YAML remains in the repository, not in Release assets or the platform package. Component archives exclude `docs/` and `test/e2e/`; aggregate preparation collects them from the selected component source tags into the platform archive (§8.1).

Component workflows build and test at the selected source SHA. Aggregate prepare downloads the six complete manifest-selected Releases, validates GitHub size/digest, component SHA-256, internal paths and cross-package collisions, then creates a deterministic platform package. Release asset validation extracts the same short-lived artifact on a real KVM runner and runs the five component-owned suites plus the platform combination suite, six owner entries in total. Only successful validation permits aggregate publish to create the tag and Release.

Preview, maintenance Stable and mainline Stable use identical asset contracts and release asset validation gates. They differ only in release state and Latest policy.

<a id="documentation-in-the-platform-package"></a>
### 8.1 Documentation payload and source mapping

The platform archive assembles documentation from the project repository and the
selected component sources. Source navigation and archive navigation use
different layouts. `test/e2e/assemble_docs.py`, called by the existing E2E
assembler, copies documentation and rewrites its links after owner suites have
been copied. It does not edit executable examples, scripts, configuration values
or component binaries.

#### Layout

| Source | Archive destination |
| --- | --- |
| Project and component `docs/*` | `docs/*`, preserving existing flat entry points |
| Component `README.md` / `README_zh.md` | `docs/<component>.md` / `docs/<component>_zh.md` |
| Component native-build, example, contribution and license documents | `docs/<component>/<original-path>` |
| Project documents outside `docs/` and `test/` | `docs/project/<original-path>` |
| Project `test/` documentation | Its existing `test/` path |
| Component `test/e2e/` documentation | `test/e2e/<component>/<original-relative-path>` |

Both language versions are included when present. Existing complete English-only
documents remain valid. License and attribution files retain their contents.
Generated build outputs, dependency/vendor trees and Git metadata are excluded.
Flat destination collisions and symbolic links in documentation inputs are
rejected rather than silently overwriting another component's document.

#### Navigation and source versions

Links to included documents, images and license files are rebased to their actual
archive paths. Reciprocal language selectors follow the renamed component
READMEs. Links to source files that are not included in the archive use GitHub
URLs for the corresponding source reference. Fenced code and inline-code
examples remain unchanged. Ordinary inline links, reference definitions and
HTML `href`/`src` attributes are handled; complex Markdown still requires review.

Directory links without a fragment use the source page's language when a
matching README is included, including component `docs/` links that fall back
to the component README. English is the fallback when no Chinese edition exists.
Direct file links and directory links with explicit fragments keep their named
file or default README target, preserving the original anchor contract.

The release packager obtains component references from the existing selected
release manifest and uses the aggregate version for project source URLs. It
passes these references only to documentation assembly; it does not alter version
selection. For direct source assembly, Git HEAD is used when available, otherwise
source links use `main`. A local acceptance run can provide a tab-separated
`DOCS_SOURCE_REFS` file containing owner and exact source revision. This is
assembly metadata, not a runtime configuration option.

The runtime and vmlinux units can select different guest-runtime commits. The
release packager therefore supplies `DOCS_VMLINUX_SOURCE` independently and takes
both `vmlinux.md` and `vmlinux_zh.md` from that selected kernel source. If an older
selected kernel has no Chinese counterpart, assembly does not substitute a
Chinese document from the runtime unit's different revision.

Recognized cross-repository `main` links to included documents resolve within the
assembled set. Absolute GitHub `main` links to other files or directories that
exist in the selected source use that source's selected reference, just like
relative source links; queries and fragments are preserved. If an absolute
`main` URL names a file absent from the selected source, it remains unchanged and
requires separate review. Explicit historical-version URLs remain historical references.
External links, including component source URLs, still require the
separate link checks described by [the review policy](../CONTRIBUTING.md#documentation-contributions).

#### Validation

```sh
python3 -m unittest release/test_documentation_package.py
make test-release-tools
```

The focused tests exercise language selectors, native-build links, source URLs,
unchanged executable content, cross-repository links, collisions, symbolic links
and independent kernel-language selection. The release tests also unpack the
actual platform tarball and check that both kernel documents came from the
selected vmlinux source. Final acceptance must additionally run assembly on the
actual reviewed source set, inspect the extracted archive, and validate all
relative paths and heading fragments. Translation completeness is a separate
semantic review; a passing package check is not evidence that pending documents
have been translated.

## 9. Preview GC

After a Stable aggregate is published, its version line enters a seven-day rollback window. `Preview GC` generates an exact allowlist from first-parent `daily-preview.yaml` history reachable from the Stable tag, not from broad version globs. It also protects:

- component Previews referenced by any other retained aggregate Preview;
- component Previews referenced by current Daily manifests on `main` or any platform maintenance branch;
- objects with active publication or deletion workflows.

Planning validates the Stable Release, tag, eight asset digests, canonical manifest, Preview ownership, Release ID, recoverable source SHA and active runs, producing a stably ordered plan and SHA-256 digest. A current Daily manifest whose same-version Stable has closed the line no longer protects those Previews. References from other open branches and retained aggregate Previews remain protected. Owning-repository deletion wrappers converge both complete and incomplete canonical Previews. Any pagination, field, reference or ownership inconsistency stops with zero deletions. Manual dry runs are not restricted by the seven-day window, but apply cannot bypass it.

Before dispatching any component deletion, apply re-reads current Daily manifests on every platform branch. A reference added since planning stops the run with zero deletions. The same supported concurrency key also serializes all platform branch coordinators and GC. After GC dispatches deletion, Daily checks every matching component/aggregate publication and deletion run before committing a new manifest. Any active run keeps the manifest unchanged. Global serialization and checks on both sides prevent manifest selection from crossing asynchronous deletion.

Historical aggregate Previews were published before the contract requiring tags to point directly to manifest commits was established and fully enforced. Only for those historical objects, GC uses first-parent manifest history reachable from a Stable tag to prove ownership and exact component selection. Release `target_commitish` must agree with the tag. If the tag commit already has a Daily manifest, it must select that exact Preview and the canonical manifest commit must lie in its first-parent history. An earlier commit without a selection manifest must instead be a first-parent ancestor of the canonical commit. Side branches, tags pointing to other manifests and moved tags without provable relationships are rejected. Current publication, incomplete recovery and all new Previews retain the strict contract that tag commit and manifest commit are identical.

Apply first dispatches the five component repositories' local deletion wrappers. Each uses only its own short-lived `GITHUB_TOKEN contents:write`, verifies the exact Preview tag and source SHA again, then deletes the Release, all assets and tag together. Platform deletes aggregate Previews only after all component candidates disappear. Partial failure does not recreate deleted objects. Deterministic component and aggregate deletion failures each receive at most three attempts; the next run recomputes canonical history and continues convergence. If a same-name object remains visible or is recreated after successful cleanup, the next apply dispatches a fresh cleanup run. Stable Releases, Stable tags, Actions artifacts and noncanonical orphans are outside the GC deletion set.

An apply run waits for asynchronous deletion and rebuilds the plan from live Releases, tags and protected references every 30 seconds, with planning time added to that interval. It continues through all eligible Stable versions and reports convergence only after each plan has no remaining candidates. The controller has a shared one-hour convergence budget for the run; pending candidates at the deadline cause failure, and a later run resumes from actual repository state. The workflow allows 75 minutes for setup and finalization. A dry run prints one plan per requested or discovered Stable version without dispatching or waiting.

```bash
# Read-only plan against actual release state.
gh workflow run preview-gc.yml --repo kuasar-sandbox/kuasar-sandbox --ref main \
  -f stable_version=release-v0.5.7 -f dry_run=true

# Converge after the seven-day window.
gh workflow run preview-gc.yml --repo kuasar-sandbox/kuasar-sandbox --ref main \
  -f stable_version=release-v0.5.7 -f dry_run=false
```

<a id="10-权限与可靠性"></a>
## 10. Permissions and reliability

- Cross-repository GitHub App access is limited to `Contents: read`, `Pull requests: read` and `Actions: write`. Each short-lived token requests only the subset required for its current step.
- Daily manifest commits use the same App's separate short-lived `Contents: write` token scoped only to the `kuasar-sandbox` repository. It has no component-repository write access and does not reuse the generic `github-actions` identity.
- Component publication, component deletion, aggregate publication and platform deletion use only their respective workflow's short-lived repository `GITHUB_TOKEN contents:write`.
- Source-fetch tokens on candidate-executing runners are read-only and revoked before execution. This does not isolate persistent App/runner credentials or shared writable state; see [CI runner slots](../ci/runner/README.md).
- Publication first creates a draft, uploads assets and verifies digests; it becomes public only after every check agrees.
- Publishers never overwrite published tags or Releases. Incomplete Preview recovery uses only the protected deletion entry.
- Branch HEAD, tag commit, manifest blob SHA and release asset validation together pin a publication.
- Even when the rendered Daily manifest needs no write, the coordinator rechecks remote branch HEAD and manifest blob rather than dispatching components from a stale checkout.
- Preview build bindings prevent incorrect reuse of the same tag/source across different dependency closures.
- The scanner has a separate concurrency key. All platform branch coordinators and GC share the global `preview-manifest-selection-and-gc` group. The scanner waits for each branch in sequence rather than filling multiple pending slots. A component's complete build/publication workflow and deletion share a mutation group for the same exact version; the platform's complete prepare/validation/publish workflow and deletion also share an exact-version group. GitHub can coalesce pending requests in a group. Daily and GC do not treat cancellation as success. Component publication/deletion rerun the appropriate jobs or full workflow with identical inputs; aggregate publication creates a new workflow run and exact stage. Each path allows at most three attempts; Daily publication or cleanup reports failure when retries are exhausted, so exclusion does not silently lose the desired publication or deletion state. Different versions do not share pending slots. Component Latest reconciliation is an exception: it is idempotent and scans the complete mainline Stable set every time, so it uses repository-wide serialization and allows triggers to coalesce. The last retained run can still converge the full state. Only the Stable aggregate selected by `release.yaml` at current platform `main` HEAD can update Latest, and branch HEAD, manifest selection and existing Release are revalidated before and after publication.

<a id="11-受保护分支"></a>
## 11. Protected branches

The required `ci / finalize` check is backed by `kuasar/ci-exact-head` on the
current two-parent integration commit. Failed, cancelled or skipped E2E cannot
produce acceptance, including a draft workflow whose control jobs succeed.
Do not substitute a result bridge or an independently supplied success status.

One repository ruleset governs project-branch protection. The active ruleset matches only `refs/heads/main` and `refs/heads/release/v*`. It requires a PR, resolved discussions, strict `ci / finalize` status checks and linear history, and blocks force pushes and branch deletion. It does not use default administrator bypass, signed-commit requirements, CODEOWNERS or a merge queue.

The current `required_approving_review_count` is 0. GitHub counts approvals only from writers other than the PR author. Without an independent write reviewer, requiring one approval would prevent maintainers' own PRs from merging under the ruleset. Review still requires complete diff inspection, resolved review threads and exact Integration E2E evidence.

Before enabling this ruleset, confirm that the `kuasar-sandbox-bms-ci` installation has approved `Contents: write`. It must not be activated without that permission: Daily Preview could not commit its converged manifest and would be blocked by the deliberately exclusive bypass design.

Status checks use `do_not_enforce_on_create: true` for a new branch, allowing a maintenance line to be created from an already-published Stable tag. Every subsequent update immediately returns to the same PR and Integration E2E gates; branch creation cannot bypass later commit checks.

Daily Preview must commit its converged manifest directly to the protected target branch. The active ruleset therefore grants `always` bypass only to the `kuasar-sandbox-bms-ci` GitHub App, ID `4283831`. That App's only write use is the repository-scoped short-lived token described above. Human maintenance, ordinary `github-actions` and all other Apps are absent from the bypass list. CI always revalidates the PR's exact integration commit and target branch; this bypass does not replace the `ci / finalize` gate for PRs. The App name is an existing registration identifier, not a validation method.

## 12. See also

- [ci.md](ci.md): CI revisions, caches and execution modes;
- [deployment.md](deployment.md): deployment and runtime prerequisites;
- [../test/QUICKSTART.md](../test/QUICKSTART.md): complete aggregate-release validation;
- [../release/](../release/): selection, packaging, coordination, recovery and GC implementation.
