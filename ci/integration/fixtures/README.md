These immutable compatibility fixtures describe `release-v0.1.6-preview.20261010.1`.
They are historical evidence, not acceptance of the tag-source release contract.

- `pre-cutover-plan.json` is the original validated integration plan. Its canonical
  SHA-256 is `35addc8b111a87289f5691f3928febc85e624cdcc1112d69889f8282e56f9560`,
  matching the published `plan_id`.
- `pre-cutover-release-body.txt` is the published body with its original binding.
- `pre-cutover-preview.yaml` is the manifest at that aggregate's source commit.

Independent historical test identities are intentionally preserved. The tests
must not interpret them as product-tag identities or fetch source by those SHAs.
The publication-size regression adds explicitly synthetic fixed-length tree
identities to measure serialization; it does not certify new source provenance.
