[English](README.md) | [简体中文](README_zh.md)

# Documentation index

Choose an entry by the task you need to complete. Platform guides retain complete workflows; component specifications own exact behavior and implementation boundaries.

## Platform guides

| Guide | Reading goal |
| --- | --- |
| [Concepts and terminology](concepts.md) | Understand instances, templates, E/S, identity and parents |
| [Architecture and capability tree](architecture.md) | Inspect capability layers, paths and authority |
| [Quick Start](quickstart.md) | Complete the first real run and cleanup |
| [Installation](installation.md) | Select, download, verify and install matching inputs |
| [Sandboxes](sandboxes.md) | Operate, access, pause/resume and reset instances |
| [Templates](templates.md) | Build, qualify, publish and reuse applications |
| [Deployment](deployment.md) | Assemble and qualify a node or cluster |
| [node-infra](node-infra.md) | Deploy, operate and recover the aggregated data-plane system container |
| [Security boundaries](security.md) | Configure identity, isolation, content protection and trusted paths |
| [Operations](operations.md) | Diagnose, recover, retain and clean up |
| [Upgrades](upgrading.md) | Qualify compatibility, change versions and roll back |
| [Performance and capacity](performance.md) | Measure, qualify capacity and tune |
| [CI](ci.md) | Cross-repository source selection and validation |
| [Release maintenance](release.md) | Component/aggregate selection, packaging and publication |

## Component contracts

| Component | Topics |
| --- | --- |
| orchestrator | [node](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node.md), [sandbox-lifecycle](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/sandbox-lifecycle.md), [node-build](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-build.md), [node-proxy](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-proxy.md), [node-resource](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-resource.md), [observability](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/observability.md), [cluster](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/cluster.md), [extensions](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/extensions.md) |
| sandboxer | [runtime](https://github.com/kuasar-sandbox/sandboxer/blob/main/docs/runtime.md), [configuration](https://github.com/kuasar-sandbox/sandboxer/blob/main/docs/configuration.md), [artifacts](https://github.com/kuasar-sandbox/sandboxer/blob/main/docs/artifacts.md), [memory](https://github.com/kuasar-sandbox/sandboxer/blob/main/docs/memory.md), [block-storage](https://github.com/kuasar-sandbox/sandboxer/blob/main/docs/block-storage.md), [resources](https://github.com/kuasar-sandbox/sandboxer/blob/main/docs/resources.md), [guest-abi](https://github.com/kuasar-sandbox/sandboxer/blob/main/docs/guest-abi.md), [vmm](https://github.com/kuasar-sandbox/sandboxer/blob/main/docs/vmm.md) |
| accelerator | [artifacts](https://github.com/kuasar-sandbox/accelerator/blob/main/docs/artifacts.md), [transfer](https://github.com/kuasar-sandbox/accelerator/blob/main/docs/transfer.md), [store](https://github.com/kuasar-sandbox/accelerator/blob/main/docs/store.md), [cache](https://github.com/kuasar-sandbox/accelerator/blob/main/docs/cache.md), [image-preparation](https://github.com/kuasar-sandbox/accelerator/blob/main/docs/image-preparation.md) |
| connector | [vswitch-operations](https://github.com/kuasar-sandbox/connector/blob/main/docs/vswitch-operations.md), [vswitch](https://github.com/kuasar-sandbox/connector/blob/main/docs/vswitch.md), [tapfd](https://github.com/kuasar-sandbox/connector/blob/main/docs/tapfd.md) |
| guest-runtime | [using-guest-runtime](https://github.com/kuasar-sandbox/guest-runtime/blob/main/docs/using-guest-runtime.md), [runtime-bundle](https://github.com/kuasar-sandbox/guest-runtime/blob/main/docs/runtime-bundle.md), [vmlinux](https://github.com/kuasar-sandbox/guest-runtime/blob/main/docs/vmlinux.md), [flatten-cli](https://github.com/kuasar-sandbox/guest-runtime/blob/main/docs/flatten-cli.md), [build](https://github.com/kuasar-sandbox/guest-runtime/blob/main/docs/build.md) |

## Development, validation and examples

- [Contribution, source workspace and build/test](../CONTRIBUTING.md)
- [Workbench environment](../workbench/README.md)
- [Test organization](../test/README.md)
- [Release validation](../test/QUICKSTART.md)
- [Complete E2B Demo](../test/demo/DEMO.md)
- [CI runner operation](../ci/runner/README.md)
- [Vulnerability reporting policy](../SECURITY.md)

Runtime and user guides are packaged with the selected version; omitted developer material links to the selected source revision. Check the actual release capabilities before applying current-source guidance to a historical release.
