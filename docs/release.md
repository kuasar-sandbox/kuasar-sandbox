[English](release.md) | [简体中文](release_zh.md)

# Release

## 1. Overview

Kuasar Sandbox separates component publication from platform aggregate publication. A component version describes an independent component delivery; an aggregate version describes a set of already-published components that passed exact-asset validation together. Neither version is derived from the other, and they need not have matching names.

The release units are:

- `accelerator`, `connector`, `sandboxer`, `orchestrator`: `vX.Y.Z`;
- `guest-runtime` runtime: `runtime-vX.Y.Z`;
- `guest-runtime` kernel: `vmlinux-vX.Y.Z`;
- platform aggregate: `release-vX.Y.Z`.

`guest-runtime` has runtime and vmlinux release units but remains one component repository. Every version line accepts the `-preview.YYYYMMDD[.N]` suffix. The optional revision `N` is a positive decimal integer without leading zeros; for example, `preview.20260914.1` is the first explicit revision of that date. Dates and revisions sort numerically, and the next date sorts after every revision of the preceding date. Preview is a GitHub prerelease; Stable is not a prerelease. Published versions are never overwritten, renamed or rebuilt.

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

New Stable and Daily manifests select only the six component **tags**. `test_revisions` and independent test/helper/document source overrides are rejected. For each owner, product source, E2E cases, runtime libraries, helpers and guides come from its selected unit tag; guest-runtime uses the runtime tag, while kernel guides use the independently selected vmlinux tag. The aggregate source provides platform tests and guides. Source checkout identity and helper provenance are recorded automatically and compared, never used as a second version selector or a bare-SHA retrieval key.

Aggregate fetching retrieves each named tag once and verifies its automatically resolved commit before and after retrieval. The source checkout and `source-records.json` are run inputs. Packaging checks the selected tag, source identity and clean tree again, and compares helper source facts to these inputs. Source links in component guides use the selected tag. Tests or guides changed only on a newer branch do not enter an aggregate that reuses an older product tag.

Component preflight admits the aggregate manifest by its trusted branch and records its identity and bytes. Publication consumes that fixed evidence, bound to preflight outputs, so unrelated branch advances do not invalidate a completed build. It still checks live Stable-line closure. Dependency restoration checks the complete preflight owner/tag/commit set; an artifact receipt cannot select replacement dependencies. New aggregate records retain their complete validation plan, including the checksum asset, and fail before submitting an oversized Release body rather than truncating evidence.

Ordinary test/document changes do not widen product input differences or force a product/kernel version. To deliver such changes, explicitly select a normally published new owner tag. Missing declarations fail explicitly; neither newer HEAD files nor fabricated declarations can fill the gap.

Historical manifests retain their real `test_revisions` evidence through a separate read-only parser. This does not authorize rebuilding a historical package or retrieving its independent test source. Historical consumers must use the published aggregate package and validated evidence by aggregate tag; missing evidence must fail explicitly rather than guessing ownership or substituting product-tag provenance.

The helper build fetches the complete Python 3.12 Demo wheel closure using the committed version/hash lock. Packaging requires both architectures of local wheels and their package/version/digest manifests; public preparation installs only those wheels without an index.

Advance `previous_preview_version` only after the current aggregate has a complete published Release. When an unpublished selection rolls over to a new date, retain its existing baseline (or keep the field absent for the first Preview). An abandoned selection may contain component tags that were never published and cannot serve as an update baseline.

After Stable `V1` is published, continued development on the same branch must first advance Daily to `V2` and set `previous_version: V1`. Advance the Stable manifest when preparing `V2`. After `V2`, advance both toward `V3`, and so on. When Daily and Stable name the same version and that Stable aggregate has already been published, the line is closed: no new Preview and no repeated Stable publication. If the same-version Stable Release exists but its assets or tag are incomplete, Daily also defers before any manifest or component changes; it does not publish a Preview alongside an incomplete Stable.

## 3. Branches and version lines

Platform `main` publishes the latest mainline version. Maintenance patches use platform `release/vMAJOR.MINOR.x`. For example, after publishing `release-v0.1.0`, a `release/v0.1.x` branch can be created from that tag. Maintenance then publishes `release-v0.1.1` and `release-v0.1.2`, while mainline can later publish `release-v0.2.0`.

If `release-v0.2.0` is not yet planned and `release-v0.1.0` immediately needs patches, `main` can first publish `release-v0.1.1` and `release-v0.1.2`, then create `release/v0.1.x` from a later patch point. Once that branch exists, mainline and maintenance each manage their own two manifests.

Platform GitHub Latest is updated only by a Stable aggregate published from platform `main`. Each component repository independently manages its own Latest. A Stable publication from component `main` records its source branch and exact commit. A separate idempotent reconciliation workflow selects Latest from all published mainline Stable releases by source-commit order, comparing SemVer only when multiple tags refer to the same commit. Any completed component publication triggers reconciliation. It is serialized across versions, can be rerun independently after failure and has scheduled self-healing. Component maintenance Stable releases and all Previews do not update Latest. Platform aggregates always select components by exact manifest tags, not by component Latest.

Platform and component maintenance branches need not have matching names. Platform `release/v0.5.x` can aggregate `sandboxer release/v0.3.x`, `orchestrator release/v0.4.x` and a fixed vmlinux version released only from `main`.

## 4. Daily branch scanning and component selection

`Daily Preview Scanner` runs daily on an Asia/Shanghai schedule, scanning:

- platform `main`;
- every platform branch matching `release/vMAJOR.MINOR.x`.

The scanner pins each branch HEAD SHA, then loads the trusted controller from platform `main` to process that SHA. The controller accesses component repositories with a read-only contents GitHub App token. The token is passed only through a subprocess-scoped Git HTTP authorization header, never through remote URLs, repository configuration or logs. All branch coordinators and Preview GC share one GitHub Actions concurrency key, preventing manifest selection from overlapping GC planning/dispatch. The scanner dispatches and waits for branches in order. Cancelled or timed-out branch tasks receive at most three attempts in that scan; later scans recover work that remains incomplete. If a branch moves during selection or manifest commit, the current run does not overwrite remote state; the next run starts from the new HEAD. A deterministic failure is recorded while other branches continue, and the scanner reports failure at the end. One broken maintenance line therefore cannot indefinitely starve other branches.

### 4.1 Source-branch mapping

- Platform `main` always selects every component repository's `main`.
- A platform maintenance branch derives the component branch from that unit's Daily-manifest version. Both `v0.3.5` and `v0.3.6-preview.*` map to component `release/v0.3.x`.
- Runtime and vmlinux independently derive `release/vX.Y.x` from `runtime-vX.Y.Z` and `vmlinux-vX.Y.Z`.
- If the derived branch is absent, that unit reuses the manifest's complete Release without automatically changing its version. If an upstream dependency changes in the current round and the unit needs rebuilding, the entire selection is deferred. It does not write an aggregate manifest combining the old downstream binary with new dependency versions.

The six units in one platform maintenance branch can therefore come from six different component version lines.

### 4.2 Tag selection

Tag selection does not choose the largest SemVer across all branch history. It walks backward from the selected branch HEAD along first-parent history. The first commit with a complete usable Release tag wins. SemVer selects the largest valid tag only among tags on that same commit. A maintenance branch also ignores tags outside its `MAJOR.MINOR` line.

The Daily manifest's configured tag participates in the same commit-position comparison:

- the commit closer to HEAD wins;
- on the same commit, the largest SemVer wins;
- a configured tag outside the selected branch's first-parent history stops selection, preventing accidental selection of a side branch or historical major version.

A winning tag is reused when the unit’s actual product/package/material inputs have not changed, even when documentation, tests or workflows moved HEAD. The kernel unit retains its existing relevant Makefile projection. If relevant inputs changed after a winning Stable tag, Patch is incremented once to produce `vX.Y.(Z+1)-preview.YYYYMMDD`. If a winning Preview is behind HEAD, its core version is retained and only the date and optional revision change. The next scan sees that Preview and does not repeatedly increment Patch.

Dependency selection compares the linked/embedded source inputs used by each consumer. An unrelated CLI or documentation change does not rebuild all reverse dependencies. Runtime tracks accelerator flatten libraries and sandbox-init inputs; other Go consumers track their linked libraries. Reused Preview bindings retain the original dependency versions and must prove any differing tuple has identical relevant source inputs. These changes do not rebuild vmlinux. If a unit already published a Preview with the requested date and revision and dependencies change again, the process defers. An operator can request a larger revision through the scanner, for example `gh workflow run daily-preview.yml --ref main -f date=20260914.1`. The controller reselects the current source and dependency closure into new tags; it does not replace complete Releases. Repeating the same request resumes it, and scheduled scans resume an unfinished maintained revision until the next Shanghai date. Revisions are explicit, never automatically incremented. Newly published components must match the aggregate’s complete date-and-revision suffix; reused versions retain their original exact provenance and dependency checks.

A code change on the selected platform branch also creates a new aggregate Preview even if all six components are reused. Workflow run names for an aggregate version bind its platform source SHA. While any matching run remains active, the controller leaves the current Daily manifest unchanged, even if a newer run was cancelled or finished. A new branch HEAD gets a new run identity; old inputs are not rerun and an in-flight manifest is not rewritten. A branch-coordination task's identity also includes its requested date, so scans on different dates at the same platform SHA cannot incorrectly reuse each other's results.

If a dependency rebuild was committed to the Daily manifest before its component Release existed, a later scan resumes that pending Preview even when the selected dependencies already equal the manifest. This applies to both Stable and Preview winners; advancing the date or revision publishes the rebuild under the newly selected suffix. Complete Releases still require the original source and dependency checks.

<a id="first-arm-initialization"></a>
### 4.3 First migration to tag-selected inputs

Make the platform source-contract mechanism available before the component producer workflows that use it. Verify that each owner source branch contains its local guide declaration and every required input, then review a **separate** aggregate manifest change selecting normal new owner tags and an aggregate version.

For Preview, use the normal component release entry points in §6 against that committed selection, in dependency order: accelerator and connector, then sandboxer, then orchestrator and runtime. Stable uses the formal coordinator's committed selection. Once the selected component releases exist, use the normal aggregate entry point in §7 and all validation gates in §8. The independently selected vmlinux tag may remain unchanged when its relevant kernel inputs are unchanged and its selected documentation is complete.

This explicit first migration is separate from the generic Daily docs-only reuse algorithm; that algorithm cannot substitute for the reviewed new-tag selection. Do not widen product-change inputs or add a forced-build rule. Historical tags and packages remain immutable under the read-only contract in §2.

### First ARM initialization

Mainline initialization is complete. [release-v0.1.5-preview.20260922.4](https://github.com/kuasar-sandbox/kuasar-sandbox/releases/tag/release-v0.1.5-preview.20260922.4) is the first published baseline with successful x86 and declared native ARM non-KVM results, bound to source/framework `69c26d2e9d7a494b3463e42c3b8c20ac1119de4f` by [aggregate run 35751885794](https://github.com/kuasar-sandbox/kuasar-sandbox/actions/runs/35751885794). Its fourteen assets preserve the tested bytes. [PR #129](https://github.com/kuasar-sandbox/kuasar-sandbox/pull/129) then activated ordinary artifact PR resolution in `dea0bc66ae766caf47f3c6684a7f426b79b730ba`. Ordinary PR/Daily use does not require another initialization or rerunning that publication; `initialize_arm` remains false by default. See the [CI coverage ledger](ci.md#7-initial-coverage-and-rollout-evidence) for the declared profiles and retained evidence.

The first dual baseline is an explicit operator action after the public-target implementation and authorized component cutovers. Dispatch `daily-preview-branch.yml` with the existing exact `platform_ref`, `platform_sha`, `date` inputs and `initialize_arm=true`. `INITIALIZE_ARM` defaults to false. Choose a legal new Preview date/revision; each historical AMD64-only unit receives a new version, built for both targets from the same exact selected source/dependency tuple. A missing maintenance source branch defers initialization instead of inventing a source version.

The aggregate stage validates the current declared x86 and native ARM non-KVM profiles; ARM KVM parity is not a prerequisite. Once published, normal PR/Daily resolution uses that exact aggregate. No historical tag, asset or SHA256SUMS is modified, and normal runs do not implicitly request initialization or fall back to full source builds. A new Stable selection likewise needs dual unit versions; historical Stable versions remain unchanged.

## 5. Recovering incomplete publication

A complete component Release must be non-draft, have a prerelease state consistent with its tag, contain only its contracted archive set and `SHA256SUMS`, and have every asset uploaded. Historical AMD64-only components/aggregates keep their two/eight-asset contracts; dual components use three assets; historical dual aggregates retain fourteen assets. New `workbench-v1` aggregates require all sixteen declared assets. Partial ARM sets are incomplete, but an old AMD64-only release is never reclassified as corrupt or deleted to initialize ARM.

Only tags with complete Releases are selection candidates. Incomplete states do not fail the entire Daily schedule:

- If a matching publication run is queued or in progress, defer and recover in a later scan.
- For an incomplete Preview owned by the current Daily manifest, first call the owning component repository's deletion workflow. Verify the exact source SHA, delete the Release, assets and tag, then rescan and follow ordinary Daily publication.
- Ignore foreign incomplete Previews and incomplete Stable releases; neither select nor automatically delete them.
- If an incomplete fixed version has no component maintenance branch, defer rather than derive a substitute version.
- Deterministic failures receive at most three attempts, then fail the branch coordinator and scanner. Recovery does not overwrite tags or manually assemble assets. Ordinary component-publication/deletion failures rerun only failed jobs. Cancelled, timed-out or similar states without failed jobs rerun the full workflow. Aggregate build/validation failures create a new workflow run with the same exact inputs and a fresh exact stage. For a partial `workbench-v1` publication at the same source, the coordinator resumes only the failed publish job after verifying its original prepare/stage/validation jobs succeeded and its immutable stage is retained; it does not rebuild images or rerun healthy validation. Missing stages and exhausted attempts fail closed. The three-attempt budget counts the sum of `run_attempt` across those runs.

When an unfinished aggregate selection advances to a new Shanghai date or an explicitly requested larger revision, the controller no longer binds the old date's tags to new component HEADs. It first deletes incomplete aggregate objects through the protected entry, then advances the manifest date and reselects. Complete component Previews in the old selection that no retained aggregate references are left for GC after the rollback window. Without a larger requested revision or a new date, the controller stays deferred. Numbered Previews use the same protected recovery and GC rules: Stable closure plus the seven-day rollback window, canonical ownership and retained references still govern deletion.

Recovery restores the Daily process; it does not patch or rebuild assets of incomplete Releases. The `incomplete` mode never deletes a complete same-name Release. When a tag is already missing but its draft or prerelease remains, recovery deletion is allowed only if `target_commitish` is a full source SHA matching the expected source. If a same-name incomplete object reappears after successful cleanup, the controller starts a new cleanup run instead of treating historical success as proof of current convergence.

## 6. Component publication CLI

The SHA fields in controller requests are automatically observed identities, not human version inputs. In CLI examples, `OWNER_REPOSITORY`, `SOURCE_REF` and `PLATFORM_REF` denote the owner repository, source branch and aggregate branch.

Component workflows always load trusted tooling from repository `main`, but require the actual source branch and its exact HEAD SHA as explicit inputs. The examples define matching repositories and branches for automatic identity lookup and dispatch; automatic Daily supplies the same inputs:

```bash
OWNER_REPOSITORY=kuasar-sandbox/accelerator
SOURCE_REF=release/v0.2.x
PLATFORM_REF=release/v0.5.x
gh workflow run release.yml \
  --repo "$OWNER_REPOSITORY" --ref main \
  -f version=v0.2.2-preview.20260831 \
  -f source_ref="$SOURCE_REF" -f source_sha="$(gh api "repos/$OWNER_REPOSITORY/git/ref/heads/$SOURCE_REF" --jq .object.sha)" \
  -f aggregate_version=release-v0.5.7-preview.20260831 \
  -f aggregate_sha="$(gh api "repos/kuasar-sandbox/kuasar-sandbox/git/ref/heads/$PLATFORM_REF" --jq .object.sha)"

OWNER_REPOSITORY=kuasar-sandbox/sandboxer
SOURCE_REF=release/v0.3.x
PLATFORM_REF=release/v0.5.x
gh workflow run release.yml \
  --repo "$OWNER_REPOSITORY" --ref main \
  -f version=v0.3.6-preview.20260831 \
  -f source_ref="$SOURCE_REF" -f source_sha="$(gh api "repos/$OWNER_REPOSITORY/git/ref/heads/$SOURCE_REF" --jq .object.sha)" \
  -f aggregate_version=release-v0.5.7-preview.20260831 \
  -f aggregate_sha="$(gh api "repos/kuasar-sandbox/kuasar-sandbox/git/ref/heads/$PLATFORM_REF" --jq .object.sha)" \
  -f accelerator_version=v0.2.2-preview.20260831 \
  -f connector_version=v0.1.9
```

Orchestrator receives `accelerator_version`, `connector_version` and `sandboxer_version`; runtime receives only `accelerator_version` and `sandboxer_version`, matching the code embedded in its image; vmlinux has no internal component-version input. Runtime and vmlinux use their own workflows. Preflight requires `source_sha` still to be the HEAD of `source_ref`. Build restores the fixed run artifact admitted from that branch, and the final tag points to its checked identity. Dependency inputs name exact, already-complete public Releases; preflight retrieves their selected tags and freezes the checked source inputs. Each architecture builds in its own native job. Publication validates and assembles both original archives without rebuilding products.

Preview additionally requires that `daily-preview.yaml/version + preview_version` at the platform commit identified by `aggregate_sha` exactly matches the aggregate version, and that `components.<unit>` exactly matches the tag being published. Preflight freezes this manifest binding; after acquiring the publication concurrency lock, publication verifies the frozen evidence and checks live Stable-line closure without refetching a newer branch manifest. Every component's release notes record source branch, source SHA and release unit. Preview notes also record the original aggregate-manifest SHA and actual dependency-version bindings. An existing Preview can be reused only when both source and dependency bindings match.

Workflow run names include source SHA and the dependency-version tuple. The controller reruns failed runs only for identical input tuples. Changed branch HEADs or dependency selections create a new dispatch rather than consuming the three recovery attempts with obsolete inputs.

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
PLATFORM_REPOSITORY=kuasar-sandbox/kuasar-sandbox
SOURCE_REF=release/v0.5.x
gh workflow run aggregate-release.yml \
  --repo "$PLATFORM_REPOSITORY" --ref main \
  -f version=release-v0.5.7 \
  -f source_ref="$SOURCE_REF" \
  -f source_sha="$(gh api "repos/$PLATFORM_REPOSITORY/git/ref/heads/$SOURCE_REF" --jq .object.sha)"
```

Before formal publication, commit `release.yaml` on the target branch, publish missing Stable component units, then trigger the aggregate from that branch's latest HEAD. A successful mainline Stable becomes Latest. A maintenance Stable remains a discoverable formal release without displacing mainline Latest. Both Stable and Preview must be directly selected by that HEAD's current manifest. Historical manifests are only for resolving existing Releases and GC; manual dispatch cannot backfill them as new aggregate publications.

The short-lived artifact produced by aggregate prepare is immutable publication evidence for that run. After a build/validation failure, redispatch the same version, source branch and source SHA so a new prepare downloads current component Releases. After a partial new-contract publication failure, let the coordinator rerun only that publish job with its successful original prerequisites and retained stage. Never rerun the full old workflow to replace its image bytes. The controller totals attempts across old and new runs with the exact run name, reporting failure after three attempts.

Local release-tool validation:

```bash
make test-release-tools
make test-ci-tools
```

## 8. Release asset validation

Each new component Release contains its x86_64 archive, aarch64 archive and `SHA256SUMS`. Runtime and vmlinux retain independent archive names. Both maintained aggregate selections declare `delivery: workbench-v1`. A new aggregate contains one architecture-neutral platform archive, twelve unchanged component archives, two native workbench Docker image archives and unified `SHA256SUMS`: sixteen explicit assets. Historical AMD64-only and dual-architecture aggregates retain their original eight/fourteen-asset contracts. The reader obtains the contract from the exact tagged source; missing new assets never selects historical compatibility.

Do not publish generated release-metadata JSON or duplicate GitHub's automatically supplied source archives. Selection YAML remains in the repository, not in Release assets or the platform package. Component archives exclude `docs/` and `test/e2e/`; aggregate assembly collects component documentation and E2E inputs from the selected owner tags into the platform archive (§8.1). Kernel documentation follows the separately selected vmlinux product source.

Component workflows build and test at the selected source SHA. Aggregate prepare downloads the six complete manifest-selected Releases, validates GitHub size/digest, component SHA-256, internal paths and cross-package collisions, then builds the selected-tag test helpers in a credential-free source step and creates a deterministic platform package. The staged bytes enter the shared public prepare → focused case → public run contract without rebuilding products or helpers. Resolution declares exact case filenames from the selected owner tags; execution uses short private state paths and reads cases directly from the staged archive without source overlays. Native x86 runs the selected cases across all nine suites on a real KVM runner; hosted ARM runs the predeclared accelerator/guest-runtime non-KVM subset. Clean-runtime preparation and selected full-suite execution retain evidence that Go, Rust and component sources are absent. Source-dependent checks are separate required jobs. Publish requires both explicit architecture results, binds their selections/source identities/asset digests in `kuasar-integration-validation`, and uploads the original archives unchanged.

Workbench preparation uses the same canonical case discovery and external-image request function as public `prepare`. The collector resolves each moving tag once for the staged request and binds raw registry manifest/config evidence to the copied Docker archives. Native builds produce one gzip-compressed Docker image archive per architecture, with the aggregate version and exact source labels. The actual compressed bytes must be **smaller than 2 GiB**; oversized or partial inputs fail before staging/publication. The image contains tools and external inputs, while the six product archives remain separate and byte-identical to upstream.

An additional native workbench gate imports those exact staged bytes and starts with empty private Docker state and no external route. On a native KVM-capable host it executes public offline prepare with the current full ordinary selection for either architecture, then reconnects only the owned bridge for run so the existing Demo guest-Internet-egress assertion remains real. GitHub’s hosted ARM runner has no KVM device: its explicit `artifact-only` workbench result validates the archive, imported image and native release inputs, records planned cases separately, and makes no system/offline execution claim. ARM’s ordinary native build and non-KVM E2E lanes remain required. Full native ARM workbench acceptance uses the published bytes and the same `--all --exclude storage.obs.sh` selection as x86, including orchestrator, builder, telemetry and the static no-libc cgroup probe. The selected Guest kernel must provide actual PMEM/EROFS `dax=always`; architecture and runtime assertions remain intact. The case list follows the exact release files, not a historical count. Older limited workbench acceptance remains valid only for its recorded cases. Private daemon isolation, actual selected KVM/UFFD/TUN/BPF operations, and owned cleanup must pass. Workbench system mode is a trusted administrator environment using Docker privileged access; custom host AppArmor/seccomp policy tools and hardening-only proofs are not deployment or publication prerequisites. Generic system startup reports unavailable hardware without claiming its product tests passed. This toolchain environment does not replace the separate compiler-free runtime gate. New native results declare `native-full`; historical `system` results keep their original selection, and hosted ARM keeps explicit `artifact-only` scope. These records bind the exact framework, test source facts, product hashes, archive/config identities, actual compressed size, compression/import time and observed peak disk usage; system results also include start time and executed-case evidence; disk observations are not quotas.

Publication copies the tested saved images to `ghcr.io/kuasar-sandbox/workbench:<aggregate-version-without-release-prefix>`, verifies both architecture config identities and the multi-architecture index, and anonymously reads back each digest-pinned image into a temporary Docker archive. The archive verifier checks every expanded layer against the config from the tested offline image, including on publication retries. Registry digests, image IDs and offline archive hashes have distinct meanings. Existing same-name tags/assets must match; retries upload missing assets without overwriting tested bytes. Cross-service publication is resumable: the GitHub draft becomes public only after the registry and complete GitHub asset set agree. A conflict fails closed. Original source history and published release bytes are not amended.

Consumers extract only the selected product architecture plus `platform-release`; `workbench-<arch>-v*.tar.gz` is imported with `docker load`, never unpacked into that directory. The [Quick Start](quickstart.md) checks the declared set and selects explicit asset categories. Workbench is optional for users downloading products, but both image assets are mandatory for a new aggregate publication.

Preview, maintenance Stable and mainline Stable use identical asset contracts and release asset validation gates. They differ only in release state and Latest policy.

<a id="documentation-in-the-platform-package"></a>
### 8.1 Documentation payload and source mapping

The platform archive uses repository-local input discovery in
`test/e2e/package_inputs.py`. Component inputs come from the selected owner/unit
tags; kernel guidance comes from the separately selected vmlinux tag. The
assembler discovers canonical cases and runtime libraries by directory convention,
and guides through each repository's `release/guide-inputs.txt`. It also includes
Demo scripts and locks, prebuilt helpers, local wheels, and thin workbench host
entrypoints. It does not copy the whole test tree or include undeclared guides.
`test/e2e/assemble_docs.py` rebases links in whole selected documents without
editing sections, executable examples, configuration values or product binaries.

#### Owner runtime input discovery

Each E2E owner maintains its runtime dependencies under `test/e2e/lib/` (the
platform-owned suite uses `test/e2e/platform/lib/`). Assembly discovers all regular
files recursively at the selected owner tag and preserves their paths under
`test/e2e/lib/<owner>/`. Adding a library, data fixture or nested dependency needs
no filename change in another repository. Canonical cases remain discovered from
`test/e2e/cases/*.sh`.

Names beginning with `test_`, Python cache directories (`__pycache__`,
`.pytest_cache`) and compiled Python cache files (`*.pyc`, `*.pyo`) are source-only
by convention and must not be runtime dependencies. Assembly rejects symlinks,
non-regular inputs, missing/empty runtime libraries and duplicate case IDs; it
does not recover omitted inputs by downloading them at execution time. Archive
validation and execution still validate the actual delivered package.

#### Repository-owned documentation selection

Each repository declares its delivered user guides in `release/guide-inputs.txt`
at the selected owner tag (the aggregate source for platform). Entries are repository-relative Markdown files,
directories (recursive Markdown discovery), or glob patterns. Blank lines and
`#` comments are ignored. Every entry must match; missing/empty declarations,
path escapes, symlinks, non-Markdown explicit inputs and overlapping entries fail
assembly. Directory discovery preserves nested paths below `docs/`.

The platform maintains only its own declaration. Components maintain their own
user/developer audience boundary; the aggregator has no per-owner filename list.
Legal material continues to use the existing LICENSE/NOTICE/COPYING conventions.
An independently versioned unit uses `docs/<unit>.md` and its optional `_zh.md`
peer from the unit's exact selected tag, including immutable older tags.

#### Layout

| Source | Archive destination |
| --- | --- |
| Project README; quickstart, deployment, system and terminology guides | `guide/README*.md`, `guide/<topic>*.md` |
| Component README and explicitly selected user contracts | `guide/<component>/<name>*.md` |
| Independently selected kernel guide | `guide/vmlinux/vmlinux*.md` |
| Project test README, QUICKSTART and Demo guide | Their existing `test/` paths |
| Accelerator and guest-runtime E2E README pairs | `test/e2e/<component>/README*.md` |
| Workbench instructions, launcher and its policies/attribution | `workbench/` |
| Project/component license, NOTICE and LICENSES material | `guide/licenses/<owner>/<original-path>` |

The component guide list covers accelerator cache/store/manifest/file artifacts,
connector TAP-FD and switch operations, guest flatten/runtime, sandbox control
and guest-init contracts, and orchestrator node/build/proxy/resource/journal/
telemetry contracts. Both language editions are included when present. Existing
complete English-only documents remain valid. The four maintained E2E guide
files remain beside the canonical flat case set.

Internal design/patch analysis, CI/release documentation, extension development,
source regression tests and experimental performance tools remain in their
source repositories. Links to these materials use the selected source reference;
following a link does not pull an excluded document into the package. Source
files are not deleted. Selected inputs must exist and be regular files; symbolic
links and destination collisions fail assembly. License and attribution texts
retain their contents; maintained Markdown license-scope navigation is rebased.
Helpers and wheels retain their existing manifests, identities and validation.
No component binaries are repackaged into the platform archive.

#### Navigation and source versions

Links to included documents, images and license files are rebased to their actual
archive paths. Reciprocal language selectors follow the component
READMEs in their new directories. Links to source files that are not included in the archive use GitHub
URLs for the corresponding source reference. Fenced code and inline-code
examples remain unchanged. Ordinary inline links, reference definitions and
HTML `href`/`src` attributes are handled; complex Markdown still requires review.

Directory links without a fragment use the source page's language when a
matching README is included, including component `docs/` links that fall back
to the component README. English is the fallback when no Chinese edition exists.
Direct file links and directory links with explicit fragments keep their named
file or default README target, preserving the original anchor contract.

The release packager obtains exact component references from the selected
manifest's `components` tags and uses the aggregate version for project source URLs.
Documentation and cases therefore refer to the same selected tag; delivering
updated invocation guidance requires explicitly selecting a normal new owner tag. These documentation references
do not change product versions or archive bytes. For direct source assembly, Git
HEAD is used when available, otherwise source links use `main`. A local acceptance run can provide a tab-separated
`DOCS_SOURCE_REFS` file containing owner and exact source revision. This is
assembly metadata, not a runtime configuration option.

The runtime and vmlinux units can select different guest-runtime commits. The
release packager therefore supplies `DOCS_VMLINUX_SOURCE` independently and takes
both `vmlinux.md` and `vmlinux_zh.md` from that selected kernel source. If an older
selected kernel has no Chinese counterpart, assembly does not substitute a
Chinese document from the separately selected runtime source.

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
actual platform tarball and check that component guidance and source links use
the selected owner tags while both kernel documents come from the selected vmlinux
source, even when those snapshots differ. Final acceptance must additionally run
assembly on the actual reviewed source set, inspect the extracted archive, and validate all
relative paths and heading fragments. Translation completeness is a separate
semantic review; a passing package check is not evidence that pending documents
have been translated.

## 9. Preview GC

After a Stable aggregate is published, its version line enters a seven-day rollback window. `Preview GC` generates an exact allowlist from first-parent `daily-preview.yaml` history reachable from the Stable tag, not from broad version globs. It also protects:

- component Previews referenced by any other retained aggregate Preview;
- component Previews referenced by current Daily manifests on `main` or any platform maintenance branch;
- objects with active publication or deletion workflows.

Planning validates the Stable Release, tag, exact historical or dual asset digests, canonical manifest, Preview ownership, Release ID, recoverable source SHA and active runs, producing a stably ordered plan and SHA-256 digest. A current Daily manifest whose same-version Stable has closed the line no longer protects those Previews. References from other open branches and retained aggregate Previews remain protected. Owning-repository deletion wrappers converge both complete and incomplete canonical Previews. Any pagination, field, reference or ownership inconsistency stops with zero deletions. Manual dry runs are not restricted by the seven-day window, but apply cannot bypass it.

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

Workbench Preview cleanup follows the same canonical version/source ownership checks. Before deleting an owned package version, it verifies repository association, immutable digest and source/version labels, and protects active publications, other live release bindings, shared tags and manifest children referenced by retained indexes. It never prunes unrelated or untagged image versions. Historical releases keep their original cleanup contract.

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
