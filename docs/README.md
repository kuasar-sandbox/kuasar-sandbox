[English](README.md) | [简体中文](README_zh.md)

# Documentation

## Start and operate

- [Quick Start](quickstart.md): run the first sandbox from one aggregate release.
- [Deployment](deployment.md): choose a topology, services, storage and operational dependencies.
- [Complete E2B demonstration](../test/demo/DEMO.md): lifecycle, networking, template fan-out and migration.
- [Aggregate release validation](../test/QUICKSTART.md): validate delivered assets rather than source-build a first demo.

## System and component contracts

- [System overview](kuasar-sandbox.md): user semantics and the five component boundaries.
- [Terminology](terminology.md): concise cross-component vocabulary; detailed field semantics remain with their owners.
- [orchestrator](https://github.com/kuasar-sandbox/orchestrator/tree/main/docs): node lifecycle, Build, resource control, Registry, Router, Placer and extensions.
- [sandboxer](https://github.com/kuasar-sandbox/sandboxer/tree/main/docs): runtime lifecycle, portable artifacts, Guest ABI and VMM integration.
- [accelerator](https://github.com/kuasar-sandbox/accelerator/tree/main/docs): Manifest, file artifacts, Store and Cache.
- [connector](https://github.com/kuasar-sandbox/connector/tree/main/docs): vSwitch design/operations and the independent TAP FD protocol.
- [guest-runtime](https://github.com/kuasar-sandbox/guest-runtime/tree/main/docs): image flattening, Runtime Bundle and Kernel.

## Develop, validate and release

- [Contribution and documentation review](../CONTRIBUTING.md#documentation-contributions).
- [Performance methodology](perf.md): timing boundaries, evidence and regression entry points.
- [CI and BMS](ci.md): exact source sets, trust boundaries and validation modes.
- [Release contract](release.md): version selection, assets, publication/recovery and documentation packaging.
- [Runner operations](../ci/runner/README.md): install and operate the validation runner.
- [Security reporting](../SECURITY.md).

English is the default; maintained Chinese editions are linked at the top of each paired document. Component access depends on repository visibility. Authenticated source review does not constitute anonymous-access acceptance.
