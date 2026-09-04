# Kuasar Sandbox Releases

[Project overview](../README_EN.md) · [Release implementation details](release.md) · [GitHub Releases](https://github.com/kuasar-sandbox/kuasar-sandbox/releases)

Kuasar Sandbox uses independent component releases and a coordinated aggregate release.

## Release model

The project has five component repositories:

- `orchestrator`
- `sandboxer`
- `accelerator`
- `connector`
- `guest-runtime`

An aggregate release selects six release units because `guest-runtime` publishes the Runtime image and VMLinux independently. The project repository records the exact selected versions, validates them together, and publishes one aggregate release containing the project platform archive, component archives, and a release-wide checksum file.

Do not infer compatibility only from similar version numbers. Use the exact component set selected by the aggregate release.

## Channels

### Stable

A stable aggregate release uses a tag such as:

```text
release-v0.1.2
```

Stable releases contain an exact source and artifact set that has completed the project's release gates and cross-component validation. Production deployments should normally select one complete stable aggregate release and preserve its version manifest.

### Preview

A coordinated preview uses a tag such as:

```text
release-v0.1.3-preview.20260904
```

Preview releases are reproducible development source sets used for integration, evaluation, and release-candidate validation. APIs, configuration, packaging, or component selection may change before the next stable release.

A Preview label describes the public distribution channel; it does not, by itself, mean that the underlying system is an early prototype or cannot support production deployment.

## Aggregate assets

The authoritative asset list is on each aggregate GitHub Release. A typical release contains:

- one project/platform archive with documentation, deployment material, demos, and aggregate tests;
- one archive for `accelerator`;
- one archive for `connector`;
- one archive for `sandboxer`;
- one archive for `orchestrator`;
- one guest Runtime archive;
- one guest VMLinux archive;
- one `SHA256SUMS` file covering the published assets.

Asset naming may evolve. Automation should read the selected release rather than hard-code a list copied from another version.

## Verification

Download all required assets from one aggregate release and verify them before extraction:

```bash
sha256sum --quiet -c SHA256SUMS
```

Never mix component archives from different aggregate releases, stable versions, or preview dates. Keep the aggregate tag and the exact component-version selection with deployment records.

For the executable release-consumption path, use the [English Quick Start](quickstart_en.md). For the complete release gate, use the [aggregate release validation guide](../test/QUICKSTART.md).

## Source traceability

After all component repositories are public, every selected component tag and source commit must be anonymously accessible. Runtime and VMLinux releases must also identify their source commit, configuration, patches, and relevant toolchain inputs.

Published stable tags and assets are immutable. Fixes are released under a new version; existing stable tags or assets must not be overwritten or rebuilt in place.

## Security fixes

Security updates may require coordinated releases across more than one component. Follow the [Security Policy](../SECURITY_EN.md) for private reporting. Release notes and GitHub Security Advisories identify affected and fixed versions when disclosure is appropriate.
