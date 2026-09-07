[English](documentation-packaging.md) | [简体中文](documentation-packaging_zh.md)

# Documentation in the platform package

The platform archive assembles documentation from the project repository and the
selected component sources. Source navigation and archive navigation use
different layouts. `test/e2e/assemble_docs.py`, called by the existing E2E
assembler, copies documentation and rewrites its links after owner suites have
been copied. It does not edit executable examples, scripts, configuration values
or component binaries.

## Layout

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

## Navigation and source versions

Links to included documents, images and license files are rebased to their actual
archive paths. Reciprocal language selectors follow the renamed component
READMEs. Links to source files that are not included in the archive use GitHub
URLs for the corresponding source reference. Fenced code and inline-code
examples remain unchanged. Ordinary inline links, reference definitions and
HTML `href`/`src` attributes are handled; complex Markdown still requires review.

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
assembled set. Explicit historical-version URLs remain historical references.
External links, including private component source URLs, still require the
separate access checks described by [the review policy](documentation-policy.md).

## Validation

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
