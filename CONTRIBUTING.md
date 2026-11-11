[English](CONTRIBUTING.md) | [简体中文](CONTRIBUTING_zh.md)

# Contributing

Thank you for contributing to the Kuasar Sandbox project repository,
`kuasar-sandbox/kuasar-sandbox`.

## Licensing of contributions

By submitting a contribution, you agree that it is licensed under the license
that applies to the files being changed. New project files without a different
explicit license declaration are contributed under the
[Apache License 2.0](LICENSE).

Only submit work that you have the right to contribute. Preserve applicable
copyright, attribution, NOTICE, and SPDX declarations when modifying
third-party or differently licensed material.

## Pull requests

Keep each pull request focused, link the relevant issue when one exists, and
describe the validation performed. Do not include credentials, private data, or
unrelated generated files.

## CI eligibility and external contributions

Anyone may open and discuss a public Fork PR. Automatic Integration E2E currently
admits ready same-repository PRs and Fork PRs whose **PR author is an active
organization member**. The membership lookup is current; `author_association`, a
review approval, or a maintainer clicking **Re-run** does not make a non-member
Fork eligible. Main-repository framework PRs use their existing read-only
self-validation path. See the [CI contract](docs/ci.md) for exact candidate checks.

A maintainer can receive an external contribution without granting the contributor
organization membership or exposing control credentials:

1. Record the original PR, target branch, and exact commits being considered. Fetch
   its public PR head from the owning upstream repository and inspect the complete
   diff, including workflow, build-script and dependency changes. Do not execute an
   unreviewed checkout with maintainer credentials or in a shared privileged environment.
2. After reviewing those commits, create a focused organization-repository branch
   from the current target. Apply only the reviewed commits with `git cherry-pick -x`
   (or an equivalent attribution-preserving import); retain the original author and
   link the original PR and source commits in the new PR. Do not overwrite existing
   branches or copy credentials into the candidate.
3. Let the adopted PR run the normal source/artifact checks and merge through the
   normal repository rules. Review the final diff and actual current head/base
   results; adoption is not validation and does not create a successful status.
4. Report the adopted PR and outcome on the original PR. Any subsequent external
   commits require a fresh review and a new validated candidate; they are not
   automatically included by the earlier adoption.

No particular review bot, additional approval count or formal review format is
required by this procedure. It does not authorize bypassing repository rules.
Control/App/release credentials remain outside candidate execution jobs.

<a id="source-workspace"></a>
## Source workspace

The standard development workspace places the project repository and five component repositories next to one another:

| Workspace directory | Responsibility |
|---|---|
| `kuasar-sandbox/` | Platform integration, tests, release coordination and shared documentation. |
| `accelerator/` | Artifact and data infrastructure. |
| `connector/` | Sandbox networking and descriptor handoff. |
| `sandboxer/` | MicroVM lifecycle and resource mechanisms. |
| `guest-runtime/` | Guest runtime bundle, kernel and image CLI delivery. |
| `orchestrator/` | Managed sandbox, node and cluster services. |

The project repository contains:

```text
docs/              System design, deployment, performance, CI, and release documents
test/e2e/          Shared runner, flat suite cases and prepared-input helpers
test/perf|demo/    Platform performance and demonstration scripts
ci/integration/    CI helpers and source-cache maintenance
ci/native-cache/   VMLinux, EROFS, Envd, RocksDB, and Cloud Hypervisor cache support
ci/runner/         Self-hosted CI runner deployment
release/           Version resolution, packaging, aggregation, and publishing
releases/          Stable and daily Preview aggregate selections
.github/workflows/ Reusable CI, aggregate-release, and daily-Preview workflows
```

<a id="build-and-test"></a>
## Build and test

Builds use environment-provided Go and inherit its `GOROOT` and `GOTOOLCHAIN` selection. Release automation requires a working `gh` on `PATH`; the project does not install, replace, or authenticate these environment tools against fixed binary digests. EROFS tools remain built from the project-selected sources, patches, and recipes.

A complete source build is driven from this project repository and consumes the five sibling component repositories:

```bash
make -C kuasar-sandbox build
make -C kuasar-sandbox test
make -C kuasar-sandbox test-e2e RELEASE_DIR=/release E2E_WORKDIR=/tmp/kuasar-prepared
make -C kuasar-sandbox perf E2E_WORKDIR=/absolute/path/to/prepared-inputs
make -C kuasar-sandbox demo PYTHON_BIN=/absolute/path/to/pinned-venv/bin/python
```

Runtime files are collected into `bin/<arch>/` according to [`release/inputs/bin-inputs.yaml`](release/inputs/bin-inputs.yaml). Each component also supports its documented standalone build. For example:

```bash
GOWORK=off make -C sandboxer build
```

### Build a local node-infra image

On a native Linux host (x86_64 or aarch64), use the six clean checkouts in the [source workspace](#source-workspace): commit or set aside local source changes first. Install the component build and packaging prerequisites, `skopeo`, and either Podman (default) or Docker. Preload the base image named in the [Containerfile](node-infra/image/Containerfile) into the selected engine (for example, `podman pull docker.io/library/ubuntu:24.04`). The image build also needs access to the configured Ubuntu package mirrors. The component packagers use the native source trees and their license materials; a copy of `bin/<arch>/` alone is insufficient.

```bash
make -C kuasar-sandbox node-infra-image
# To build with Docker instead:
make -C kuasar-sandbox node-infra-image NODE_INFRA_ENGINE=docker
```

The target runs the platform build and writes an OCI archive, a Docker archive, and build records to a new directory under `kuasar-sandbox/build/node-infra/<arch>/`. It does not publish the image. For a Docker test, load the generated Docker archive with `docker load -i <path-to-docker.tar.gz>` and use the reported `localhost/node-infra:local-<hash>` tag.

### Test the build

Product E2E consumes prebuilt products through `test/e2e/e2e prepare` and `run`. Full `<suite>.<case>.sh` filenames identify cases across nine suites; component ownership only determines where source cases are maintained. Preparation acquires immutable images and the pinned Demo SDK; execution uses prepared products, helpers and images without source discovery, compilation or automatic pulls. Unit/race/vet, UFFD and performance gates stay independent. See [test organization](test/README.md) and the [release validation guide](test/QUICKSTART.md).

Release-tool tests do not contact GitHub or build the real native dependencies:

```bash
make -C kuasar-sandbox test-release-tools
make -C kuasar-sandbox test-ci-tools
make -C kuasar-sandbox test-perf-tools
```

Tests requiring KVM, root, eBPF, systemd, external storage, or the complete source workspace document those prerequisites separately. A skipped privileged suite must not be interpreted as a completed integration validation.

<a id="documentation-language-and-review-policy"></a>
## Documentation contributions

Organize documentation around one complete user task or component contract. Merge fix-specific notes into their owning specification; remove obsolete one-off reports instead of relocating them to a new archive hierarchy. Add or split a document only when it has a genuinely independent responsibility; a feature, fix, package or flag alone does not justify a separate file. Historical evidence does not require retaining one-off report files; original Git/PR/Issue history can preserve it. Record moved sections and removed duplication in the PR, preserve both language editions, and validate source and packaged navigation.

<a id="documentation-organization"></a>
### Organization and authoritative ownership

Start with the capability and reader task, assign its owner, then choose files.
The [architecture map](docs/architecture.md#configuration-and-contract-ownership)
and [documentation index](docs/README.md) record the current boundaries.

| Location | Responsibility |
| --- | --- |
| Project `docs/` | Platform concepts/architecture and complete cross-component user, deployment, operations and upgrade tasks |
| Component `docs/` | Capability/object contracts: use, public interfaces, exact configuration, state, failure boundaries and implementation as appropriate |
| Root README | Component/project scope, a short first-use path and topic navigation |
| CONTRIBUTING | Source workspace, build/test entry, contribution and review workflow |
| `examples/`, `test/`, `native-deps/` README | Instructions and prerequisites for the executable material beside them |
| `release/` declarations | Delivered guide selection and explicit navigation compatibility; see the [release contract](docs/release.md#versioned-navigation-migrations) |

An independent file needs a complete task or stable object/contract, a clear reader
and owner, and an explicit boundary with adjacent topics. A package, flag, process,
bug or page length is not enough. Runtime memory, block storage and resource
control use the same subsystem scale; specific implementations are chapters.
API and CLI bindings can share a capability's document rather than creating two
independent feature specifications. Configuration syntax/defaults have one owner;
mechanism documents explain behavior without copying the complete schema.

Keep `docs/` shallow and use stable English topic names. Begin a document with
scope, prerequisites and its authoritative boundary. Order content for its task:
concepts, a complete example, precise reference, mechanisms/failures and validation
where useful. Use semantic headings and links, not a second manually maintained
section-number index. End related links after substantive content.

Keep exact state transitions, wire versions, format fields and defaults in one
authoritative topic. Guides retain executable examples and necessary explanation.
For a structural change, map every old maintained section to a target or identify
it as obsolete/duplicated with evidence; retiring a path does not permit losing
its requirements or Chinese edition. Update declarations and test assembled
navigation with both current and independently selected historical component
inputs. Product behavior and release policy are outside a documentation rewrite.

### Scope

Every maintained first-party project, design, deployment, build, operations, validation and example document must provide complete English content. Existing English-only documents are valid. When translating a Chinese document, preserve its complete Chinese version beside the English default: `name.md` and `name_zh.md`. Both files begin with reciprocal language links. Do not create a new documentation hierarchy or duplicate upstream legal texts to satisfy this policy.

### Translation contract

Read the whole source document at a recorded commit before translating. A translation-only change preserves its organization, section numbering, tables, diagrams, examples, limitations and failure behavior. A summary or an English abstract is not a translation of a specification. Long documents may use separate review commits, but the default document must be complete before its translation PR is merged.

Keep API and CLI names, configuration keys and values, state names, format fields, constants, paths, units and executable examples unchanged unless a separately identified source-backed correction is required. Translate explanatory comments and diagram labels without changing program or graph semantics. Detailed design documents retain the protocol and implementation terminology that a concise project overview may omit.

Translate normative requirements without strengthening or weakening them: MUST, MUST NOT, SHOULD, SHOULD NOT and MAY retain their respective meanings. An implementation observation must not become a universal requirement or compatibility promise merely through translation.

### Structural cleanup

An explicitly approved structural cleanup may retire old document paths, remove forwarding-only sections and migration aliases, and reorder section numbers. Preserve the complete maintained contracts in their owning documents, update both language editions, and migrate every maintained inbound reference to the actual topic. Old URLs and section numbers do not require permanent compatibility pages or aliases. This does not alter upstream legal texts, real protocol compatibility boundaries or historical evidence.

### Correcting existing documentation

Use the implementation owned by the relevant component, pinned build inputs and recorded validation evidence to check disputed claims. Describe factual corrections separately from translation in the PR. Apply verified corrections to both languages. When the available source does not establish a claim, mark the uncertainty or remove an unsupported claim; do not replace it with an invented fact.

Distinguish implemented behavior, proposed design and historical measurements. Preserve the environment and revision of historical reports. Do not implement product changes, rerun unrelated benchmarks, disable security mechanisms or alter release policy as part of a language change. Upstream licenses and attribution remain unchanged.

### Navigation and revisions

English navigation should target English default paths. Chinese navigation should target the Chinese counterpart where it exists, otherwise identify an English-only destination. Update relative links, reference-style links, heading fragments, explicit anchors and numbered cross-references. For translation-only changes, preserve externally referenced anchors where practical. For an approved structural cleanup, follow the approved retirement scope and migrate maintained references instead of retaining obsolete paths or numbering.

Check repository source navigation and assembled platform documentation separately: a path valid in a six-repository workspace is not automatically valid in an archive. Use explicit cross-repository source URLs where no repository-local target exists. Do not create links containing private download tokens.

Record the original commit, affected paths, implementation PR and validation. Before merge, compare the source against the current target branch and incorporate intervening semantic changes in both languages. Do not overwrite newer Chinese content with an older copied baseline. Future semantic changes should update both maintained language versions in the same PR.

### Automated checks

Run the standard-library checker without network access:

```sh
python3 -m unittest discover -s ci -p 'test_check_docs.py'
: "${BASE_COMMIT:?Set the reviewed base commit SHA}"
python3 ci/check_docs.py --root . --changed-base "$BASE_COMMIT"
python3 ci/check_docs.py --root . --json
```

The changed-base mode checks only changed Markdown files and their reciprocal selectors, so unrelated documents are not included in a focused change check. The full-tree mode reports outstanding debt and is required for final acceptance. A focused check does not exempt other maintained documents from the policy.

The checker covers ordinary Markdown inline/reference destinations, local file existence, heading fragments, counterparts, selectors and unintended Chinese prose outside code fences. It does not make network requests or prove semantic completeness. Review complex Markdown constructs and executable examples separately. A narrowly scoped `<!-- docs:allow-han -->` comment may explain intentional Chinese on one English-document line; reviewers must examine every exception. Code fixtures intentionally exercising Unicode, language selectors, upstream licenses and unmodified third-party material are not untranslated prose.

### Review and completion

A PR records Summary, Corrections, Translation coverage, Validation and Out of scope. Inspect the final diff, all review discussions and required checks against the exact head and current base. Use normal repository merge rules; no administrative bypass or invented reviewer approval.

Final acceptance records file-level coverage, semantic review, links, source/archive layouts and merge evidence separately. A successful language scan does not imply a correct translation. An unavailable test is not passed. Published Release assets are immutable; ordinary documentation work never replaces them.
