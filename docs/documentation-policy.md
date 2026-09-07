[English](documentation-policy.md) | [简体中文](documentation-policy_zh.md)

# Documentation language and review policy

## Scope

Every maintained first-party project, design, deployment, build, operations, validation and example document must provide complete English content. Existing English-only documents are valid. When translating a Chinese document, preserve its complete Chinese version beside the English default: `name.md` and `name_zh.md`. Both files begin with reciprocal language links. Do not create a new documentation hierarchy or duplicate upstream legal texts to satisfy this policy.

The full-coverage work is tracked by [#86](https://github.com/kuasar-sandbox/kuasar-sandbox/issues/86); the completed minimum-entry task #83 has a narrower scope. See the [inventory](documentation-inventory.md) and [terminology](terminology.md).

## Translation contract

Read the whole source document at a recorded commit before translating. Preserve its organization, section numbering, tables, diagrams, examples, limitations and failure behavior. A summary or an English abstract is not a translation of a specification. Long documents may use separate review commits, but the default document must be complete before its translation PR is merged.

Keep API and CLI names, configuration keys and values, state names, format fields, constants, paths, units and executable examples unchanged unless a separately identified source-backed correction is required. Translate explanatory comments and diagram labels without changing program or graph semantics. Detailed design documents retain the protocol and implementation terminology that a concise project overview may omit.

Translate normative requirements without strengthening or weakening them: MUST, MUST NOT, SHOULD, SHOULD NOT and MAY retain their respective meanings. An implementation observation must not become a universal requirement or compatibility promise merely through translation.

## Correcting existing documentation

Use the implementation owned by the relevant component, pinned build inputs and recorded validation evidence to check disputed claims. Describe factual corrections separately from translation in the PR. Apply verified corrections to both languages. When the available source does not establish a claim, mark the uncertainty or remove an unsupported claim; do not replace it with an invented fact.

Distinguish implemented behavior, proposed design and historical measurements. Preserve the environment and revision of historical reports. Do not implement product changes, rerun unrelated benchmarks, disable security mechanisms or alter release policy as part of a language change. Upstream licenses and attribution remain unchanged.

## Navigation and revisions

English navigation should target English default paths. Chinese navigation should target the Chinese counterpart where it exists, otherwise identify an English-only destination. Update relative links, reference-style links, heading fragments, explicit anchors and numbered cross-references. Preserve existing externally referenced anchors where practical, using explicit aliases when translating headings would break them.

Check repository source navigation and assembled platform documentation separately: a path valid in a six-repository workspace is not automatically valid in an archive. Use explicit cross-repository source URLs where no repository-local target exists. Do not create links containing private download tokens.

Record the original commit, affected paths, implementation PR and validation. Before merge, compare the source against the current target branch and incorporate intervening semantic changes in both languages. Do not overwrite newer Chinese content with an older copied baseline. Future semantic changes should update both maintained language versions in the same PR.

## Automated checks

Run the standard-library checker without network access:

```sh
python3 -m unittest discover -s ci -p 'test_check_docs.py'
python3 ci/check_docs.py --root . --changed-base <base-commit>
python3 ci/check_docs.py --root . --json
```

The changed-base mode checks only changed Markdown files and their reciprocal selectors, so migration debt elsewhere does not block unrelated changes. The full-tree mode reports outstanding debt and is required for final acceptance. The inventory is not an exemption list.

The checker covers ordinary Markdown inline/reference destinations, local file existence, heading fragments, counterparts, selectors and unintended Chinese prose outside code fences. It does not make network requests or prove semantic completeness. Review complex Markdown constructs and executable examples separately. A narrowly scoped `<!-- docs:allow-han -->` comment may explain intentional Chinese on one English-document line; reviewers must examine every exception. Code fixtures intentionally exercising Unicode, language selectors, upstream licenses and unmodified third-party material are not untranslated prose.

## Review and completion

A PR records Summary, Corrections, Translation coverage, Validation and Out of scope. Inspect the final diff, all review discussions and required checks against the exact head and current base. Use normal repository merge rules; no administrative bypass or invented reviewer approval.

Final acceptance records file-level coverage, semantic review, links, source/archive layouts and merge evidence separately. A successful language scan does not imply a correct translation. An unavailable test is not passed. Anonymous access to private repositories stays explicitly pending the coordinated public window; a documentation task does not authorize changing repository visibility or replacing published release assets.
