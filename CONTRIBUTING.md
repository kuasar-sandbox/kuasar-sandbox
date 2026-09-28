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

<a id="documentation-language-and-review-policy"></a>
## Documentation contributions

Organize documentation around one complete user task or component contract. Merge fix-specific notes into their owning specification; remove obsolete one-off reports instead of relocating them to a new archive hierarchy. Add or split a document only when it has a genuinely independent responsibility; a feature, fix, package or flag alone does not justify a separate file. Historical evidence does not require retaining one-off report files; original Git/PR/Issue history can preserve it. Record moved sections and removed duplication in the PR, preserve both language editions, and validate source and packaged navigation.

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
python3 ci/check_docs.py --root . --changed-base <base-commit>
python3 ci/check_docs.py --root . --json
```

The changed-base mode checks only changed Markdown files and their reciprocal selectors, so unrelated documents are not included in a focused change check. The full-tree mode reports outstanding debt and is required for final acceptance. A focused check does not exempt other maintained documents from the policy.

The checker covers ordinary Markdown inline/reference destinations, local file existence, heading fragments, counterparts, selectors and unintended Chinese prose outside code fences. It does not make network requests or prove semantic completeness. Review complex Markdown constructs and executable examples separately. A narrowly scoped `<!-- docs:allow-han -->` comment may explain intentional Chinese on one English-document line; reviewers must examine every exception. Code fixtures intentionally exercising Unicode, language selectors, upstream licenses and unmodified third-party material are not untranslated prose.

### Review and completion

A PR records Summary, Corrections, Translation coverage, Validation and Out of scope. Inspect the final diff, all review discussions and required checks against the exact head and current base. Use normal repository merge rules; no administrative bypass or invented reviewer approval.

Final acceptance records file-level coverage, semantic review, links, source/archive layouts and merge evidence separately. A successful language scan does not imply a correct translation. An unavailable test is not passed. Published Release assets are immutable; ordinary documentation work never replaces them.
