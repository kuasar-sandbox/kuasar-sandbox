[English](README.md) | [简体中文](README_zh.md)

# Documentation

## User and system documentation

- [Quick Start](quickstart.md)
- [System overview](kuasar-sandbox.md)
- [Deployment](deployment.md)
- [Performance methodology](perf.md)
- [Release contracts](release.md)
- [CI and BMS](ci.md)
- [Full release validation](../test/QUICKSTART.md)
- [Demo](../test/demo/DEMO.md)
- [Runner operations](../ci/runner/README.md)

Detailed design and operations documents are being migrated to full English coverage under [#86](https://github.com/kuasar-sandbox/kuasar-sandbox/issues/86). The [file-level inventory](documentation-inventory.md) and linked implementation issues distinguish completed work from pending translation; an English navigation label alone does not imply the target is already translated.

## Component specifications

Each independently evolving component owns its detailed documentation:

- [orchestrator](https://github.com/kuasar-sandbox/orchestrator/tree/main/docs): node and cluster control plane, routing and resources.
- [sandboxer](https://github.com/kuasar-sandbox/sandboxer/tree/main/docs): lifecycle, artifacts, guest ABI and VMM patches.
- [accelerator](https://github.com/kuasar-sandbox/accelerator/tree/main/docs): data access, Manifest, Store and Cache.
- [connector](https://github.com/kuasar-sandbox/connector/tree/main/docs): vSwitch and TAP FD protocol.
- [guest-runtime](https://github.com/kuasar-sandbox/guest-runtime/tree/main/docs): runtime bundle, kernel and flattening.

Repository access follows the current coordinated source-publication state. Private links are not evidence of completed anonymous validation.

## Maintaining documentation

- [Language and review policy](documentation-policy.md)
- [Terminology](terminology.md)
- [Migration inventory](documentation-inventory.md)
- [Documentation packaging](documentation-packaging.md)
- [Contribution rules](../CONTRIBUTING.md)
- [Security reporting](../SECURITY.md)
