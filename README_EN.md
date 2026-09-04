# Kuasar Sandbox

[简体中文](README.md)

Kuasar Sandbox is a production-ready MicroVM sandbox platform for AI agents, serverless workloads, and reinforcement-learning environments. It combines an independent guest kernel boundary with snapshot-based lifecycle management, on-demand data access, high-density resource governance, sandbox-level networking, and single-node or multi-node orchestration.

> The project repository provides system-level documentation, cross-component validation, and aggregate releases. The five component repositories remain loosely coupled and can evolve, release, and be deployed independently.

## Why Kuasar Sandbox

Agent workloads execute generated code, install dependencies, modify files, start services, and access external systems. Their execution environment therefore needs stronger isolation than a shared host kernel, while still supporting rapid creation, long-lived state, pause and resume, and efficient use of node resources.

Kuasar Sandbox provides:

- **MicroVM isolation** — each sandbox runs with an independent guest kernel.
- **Snapshot-template fan-out (1:N)** — initialize an environment once, then create multiple isolated instances that share an immutable parent layer and keep independent writable state.
- **Instance pause and resume (1:1)** — preserve the memory, process, and filesystem state of a logical sandbox and resume it later, including after placement on another node.
- **Flexible data paths** — use local files, shared file storage such as NAS/NFS, or manifest/object-storage paths with layered caches according to the deployment.
- **On-demand loading** — fetch memory pages and disk blocks when the workload actually accesses them rather than requiring full eager reads.
- **High-density resource governance** — return idle CPU and inactive memory to the node while admission control, dynamic budgets, reclamation, and safety reserves protect simultaneously active workloads.
- **Sandbox-level networking** — provide fast network allocation, kernel data-plane forwarding, default sandbox isolation, trusted sandbox identity, and integration with external policy gateways.
- **E2B-compatible and native integration paths** — use existing E2B SDK workflows or integrate directly with node and cluster services.

## Architecture

```text
Agent / E2B SDK / Platform API
              │
      orchestrator
   node-ctl / cluster-ctl
              │
         sandboxer
     MicroVM lifecycle
       ├──────────── accelerator
       │             data, storage, encryption, cache
       ├──────────── connector
       │             high-density networking and identity
       └──────────── guest-runtime
                     guest kernel and runtime image
              │
 KVM / local storage / NAS / object storage / network
```

## Repositories

| Repository | Responsibility |
| --- | --- |
| [`kuasar-sandbox`](https://github.com/kuasar-sandbox/kuasar-sandbox) | System design, cross-component validation, demos, aggregate release selection, and project entry point |
| [`orchestrator`](https://github.com/kuasar-sandbox/orchestrator) | E2B-compatible node service, resource admission, data-plane proxying, and multi-node Registry/Router/Placer control plane |
| [`sandboxer`](https://github.com/kuasar-sandbox/sandboxer) | MicroVM creation, snapshot and restore, guest control, memory, block-device, and native-exec lifecycle |
| [`accelerator`](https://github.com/kuasar-sandbox/accelerator) | Data access, sparse artifacts, encryption, local/shared/object storage, manifests, caches, OCI pull, and image flattening |
| [`connector`](https://github.com/kuasar-sandbox/connector) | High-density eBPF networking, sandbox isolation and identity, and policy-gateway integration foundation |
| [`guest-runtime`](https://github.com/kuasar-sandbox/guest-runtime) | Guest kernel, runtime image, and image-building tools; produces separate Runtime and VMLinux release units |

The table describes one project repository and five component repositories. Aggregate releases contain six component release units because `guest-runtime` publishes Runtime and VMLinux separately.

## Get started

- [English Quick Start](docs/quickstart_en.md)
- [中文快速开始](docs/quickstart.md)
- [System architecture](docs/kuasar-sandbox.md)
- [Deployment guide](docs/deployment.md)
- [Demo](test/demo/DEMO.md)
- [Aggregate release validation](test/QUICKSTART.md)
- [Releases](https://github.com/kuasar-sandbox/kuasar-sandbox/releases)

The Quick Start uses assets from one aggregate release. Do not mix component archives from different aggregate versions or preview dates.

## Release channels

- **Stable** aggregate releases select exact component versions that have passed the project release gates and cross-component validation.
- **Preview** releases expose a coordinated development source set and may change before the next stable release.
- **Proposed** capabilities are designs tracked in issues or RFCs and are not part of the delivered feature set until implemented and released.

The public release channel and production readiness describe different properties: Preview marks may indicate API or packaging evolution, while the platform itself is designed and operated for production deployment.

## Build and contribute

For source development, clone this repository and the five component repositories as siblings in the same organization workspace. Each component documents its standalone build and test path; this repository owns the aggregate build, BMS, end-to-end tests, demos, and release selection.

Read:

- [Contribution guide](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Release process](docs/release.md)

Ordinary component changes should stay in the owning repository. Cross-repository protocol or integration changes should identify companion pull requests and the exact source set used for validation.

## Security

Do not report vulnerabilities in public issues. Follow the [Security Policy](SECURITY.md) and use GitHub's private vulnerability reporting path for the project repository.

## License

Kuasar Sandbox project-owned code is licensed under the [Apache License 2.0](LICENSE). Individual repositories also document the boundaries of third-party code, Linux/eBPF material, Cloud Hypervisor patches, guest-kernel sources, and redistributed runtime components.
