# First tag-source cutover

[简体中文](tag-source-cutover_zh.md)

Owner tags that predate their repository-local `release/guide-inputs.txt`
declarations cannot produce a new aggregate under the tag-source contract. Borrowing a declaration or content from main would give
that aggregate false provenance.

Use an explicit release selection for this first migration. It is separate from
the Daily related-product-input algorithm:

1. Land the source-contract implementation in platform, followed by the component
   producer workflow changes that use its `release/producer-inputs.py` tool.
   Verify the normal owner source branches contain the already-merged local guide
   declarations and every declared input. Choose new normal component tags for
   accelerator, connector, sandboxer, orchestrator and runtime. Keep the chosen
   independent vmlinux tag if its existing kernel documentation is complete.
2. Review a **separate** aggregate manifest change selecting those new tags and a
   new aggregate version. This mechanism change does not alter chosen versions.
   Stable uses the existing formal coordinator's explicit committed selection.
   Preview uses the ordinary component release workflows against that committed
   Preview selection, in dependency order: accelerator and connector, sandboxer,
   then orchestrator and runtime. Each request selects an owner branch; its source
   identity and the aggregate branch identity are read automatically from GitHub
   and checked by preflight. No human commit lookup key is needed. Do not run the
   generic Daily selector to substitute a docs-only version bump for this explicit
   selection before the normal component tags have been published.
3. Once those normal component releases exist, run the ordinary aggregate path
   for the reviewed manifest. Fetching uses the newly selected tags. The normal
   source inspection, repository-local guide discovery, helper checks, package
   inventory checks and both architecture gates remain required. A missing
   declaration fails before publication. The first aggregate has self-contained
   validation-plan and owner/source evidence for subsequent consumers.

This creates real new component releases; it does not relabel old sources or
mutate old tags/packages. There is no generic forced-build switch and no guide
path is added to product-change inputs. After migration, ordinary docs/test edits
continue to reuse product versions until a later explicit or product-driven tag
is selected. PR tests still use admitted candidate sources with reused binaries.

Deterministic tests exercise missing declarations at an old tag, refusal to borrow
newer HEAD content, acceptance of a normal new tag with unchanged product inputs,
and named-branch producer admission followed by identical artifact restoration
for both architectures. Historical test provenance remains unchanged; an older
aggregate without recoverable original ownership evidence cannot be represented
as having tag-derived tests.
For an owner without an admitted candidate, source CI also rejects a selected tag
that omits any case ID in the validated baseline. Such a mismatch requires an
explicit new-tag cutover; it cannot be resolved by silently reducing coverage.
