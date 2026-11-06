[English](README.md) | [简体中文](README_zh.md)

# Kuasar Sandbox

Kuasar Sandbox is a MicroVM sandbox platform for AI agents, serverless workloads,
code execution and reinforcement-learning environments. Each sandbox has an
independent Guest kernel. The platform combines reusable templates, stateful
pause/resume, on-demand data loading and node resource governance.

## Start here

| Goal | Guide |
| --- | --- |
| Run the first real sandbox | [Quick Start](docs/quickstart.md) |
| Obtain a matching version set | [Installation](docs/installation.md) |
| Build an application template | [Templates](docs/templates.md) |
| Execute work and manage retained state | [Sandboxes](docs/sandboxes.md) |
| Understand capabilities and objects | [Concepts](docs/concepts.md), [architecture and feature tree](docs/architecture.md) |
| Deploy and operate the platform | [Deployment](docs/deployment.md), [operations](docs/operations.md), [security boundaries](docs/security.md) |
| Change versions or assess capacity | [Upgrades](docs/upgrading.md), [performance](docs/performance.md) |

## Capabilities and component responsibilities

The platform creates independent instances from read-only template parents, or
pauses and later resumes one logical instance. Memory snapshots preserve process
state; disk-only saved state restarts processes. Explicit parent relationships
allow sharing while each instance keeps its identity and writable changes.
Portable state still requires compatible execution inputs, complete dependencies,
keys, network ownership and resource admission at the destination.

Local files, named shared filesystems and Manifest/object storage are supported
data paths. Cache accelerates access; it does not replace durable artifact
retention. Host-established network identity and scoped credentials separate
platform users and Guest workloads. External gateways own their selected policy.

| Component | Responsibility |
| --- | --- |
| [orchestrator](https://github.com/kuasar-sandbox/orchestrator) | Managed lifecycle, Build, access, node admission, observability, extensions and Registry/Router/Placer control |
| [sandboxer](https://github.com/kuasar-sandbox/sandboxer) | Runtime, E/S artifacts, memory, block storage, local resource execution and Guest/VMM integration |
| [accelerator](https://github.com/kuasar-sandbox/accelerator) | Generic data contracts, transfer, durable Store, Cache and image-production libraries |
| [connector](https://github.com/kuasar-sandbox/connector) | eBPF/TC networking, isolation, trusted identity, gateway integration and TAPFD handoff |
| [guest-runtime](https://github.com/kuasar-sandbox/guest-runtime) | Guest Runtime bundle, independently versioned VMLinux, flatten CLI and native delivery inputs |

This project repository owns cross-component design, validation, development
infrastructure and aggregate releases. Components remain independently usable.
The [feature tree](docs/architecture.md#capability-tree-and-authoritative-topics)
records each capability's authoritative documentation and implementation boundary.

## First application

Follow [Quick Start](docs/quickstart.md) to acquire and verify one aggregate and run
the actual Demo in a matching Workbench. Workbench supplies the private development
and test environment; native production deployment is independent of it. A
completed Demo stops its temporary node. For continued use, retain an explicitly
running service and its connection settings, then use the complete
[template](docs/templates.md) and [instance](docs/sandboxes.md) examples.

Success is a real template/create/command/file/pause/resume/delete cycle. A
download, process start or skipped privileged test does not establish that result.
The [full validation guide](test/QUICKSTART.md) covers aggregate E2E beyond the Demo.

## Versions and support scope

Select one [Stable release](https://github.com/kuasar-sandbox/kuasar-sandbox/releases/latest)
or explicit Preview and retain its exact tag. An aggregate selects six component
release units: accelerator, connector, sandboxer, orchestrator, Runtime and
VMLinux. Their versions can differ. New aggregates provide native Linux x86_64
and aarch64 inputs; historical releases retain their published architecture set.

Stable/Preview describe release channels. Production deployments still qualify
capacity and configure TLS, durable storage, network policy and credentials.
Source features, proposals and measured results are distinguished in the
[architecture](docs/architecture.md) and [performance](docs/performance.md) guides.
See [release maintenance](docs/release.md) for selection and publication, and
[upgrades](docs/upgrading.md) for the effect on retained workloads.

## Documentation

The [documentation index](docs/README.md) provides all platform tasks and component
contracts. English uses the default filename; maintained Chinese counterparts use
`_zh.md`. Existing English-only documents remain valid.

For development, use the [source workspace and build/test guide](CONTRIBUTING.md#source-workspace),
[Workbench](workbench/README.md), [test organization](test/README.md) and [CI](docs/ci.md).
Benchmark methods and versioned evidence are in [performance](docs/performance.md).

## Contributing and security

Read [CONTRIBUTING](CONTRIBUTING.md) and choose the repository that owns the
behavior. Keep cross-repository changes coordinated through explicit companion
references. Report vulnerabilities privately through [SECURITY](SECURITY.md).

## License

Original project content is licensed under [Apache License 2.0](LICENSE).
Third-party and differently licensed material retains its declarations and
attribution. [CONTRIBUTING](CONTRIBUTING.md) explains contribution licensing.
