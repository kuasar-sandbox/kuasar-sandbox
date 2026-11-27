[English](README.md) | [简体中文](README_zh.md)

# Kuasar Sandbox

Kuasar Sandbox is a MicroVM sandbox platform for AI agents, serverless workloads,
code execution and reinforcement-learning environments. Each sandbox has an
independent Guest kernel. The platform combines reusable templates, stateful
pause/resume, on-demand data loading and node resource governance.

## Run a sandbox with Docker

Use a native Linux x86_64 or aarch64 host with a local rootful Docker Engine,
cgroup v2, KVM/TUN and the [required kernel capabilities](docs/node-infra.md#prerequisites-and-release-inputs).
Replace `<aggregate-tag>` below with the exact tag from your selected
[Release](https://github.com/kuasar-sandbox/kuasar-sandbox/releases), retaining
`release-`. Use an unused container name and a new dedicated volume.

```bash
RELEASE_VERSION='<aggregate-tag>'
IMAGE="ghcr.io/kuasar-sandbox/node-infra:$RELEASE_VERSION"
docker pull "$IMAGE"
docker run -d --tty --name ks-dev --pull=never --privileged \
  --cgroupns=private --cpus=4 --memory=6g --memory-swap=6g \
  --tmpfs /run --tmpfs /run/lock --tmpfs /tmp:exec --stop-timeout=420 \
  --network=bridge -v ks-dev-data:/var/lib/node-infra "$IMAGE" quickstart
docker exec ks-dev node-infra-run -- /bin/sh -c 'printf "hello from guest\n"'
```

Expect `hello from guest` from a real MicroVM. The image supplies the node,
Guest boot inputs and a small shell filesystem. First startup generates private
credentials for the dedicated volume.
`node-infra-run` waits for readiness and deletes the Guest after the command;
the node stays running for the next command. To open an interactive Guest shell,
use `docker exec -it ks-dev node-infra-run -- /bin/sh` and type `exit` to finish.

The 4 CPU/6 GiB budget is for this trial. Public API/data ports and ordinary
Guest Internet access are not enabled by the profile. Follow
[Quick Start](docs/quickstart.md) for host checks, health, stop/restart and cleanup;
use [site deployment](docs/deployment.md#production-start) for your own images,
SDK clients and network policy.

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
platform users and Guest workloads. The native egress gateway or an operator-provided gateway owns outbound policy;
API and application ingress remain separate paths.

| Component | Responsibility |
| --- | --- |
| [orchestrator](https://github.com/kuasar-sandbox/orchestrator) | Managed lifecycle, Build, access, node admission, observability, extensions and Registry/Router/Placer control |
| [sandboxer](https://github.com/kuasar-sandbox/sandboxer) | Runtime, E/S artifacts, memory, block storage, local resource execution and Guest/VMM integration |
| [accelerator](https://github.com/kuasar-sandbox/accelerator) | Generic data contracts, transfer, durable Store, Cache and image-production libraries |
| [connector](https://github.com/kuasar-sandbox/connector) | eBPF/TC networking, isolation, trusted identity, native Geneve egress and TAPFD handoff |
| [guest-runtime](https://github.com/kuasar-sandbox/guest-runtime) | Guest Runtime bundle, independently versioned VMLinux, flatten CLI and native delivery inputs |

This project repository owns cross-component design, validation, development
infrastructure and aggregate releases. Components remain independently usable.
The [feature tree](docs/architecture.md#capability-tree-and-authoritative-topics)
records each capability's authoritative documentation and implementation boundary.

## From the first Guest to your application

1. [Run a Guest command](docs/quickstart.md) with the published node-infra image
   and an empty private volume. The node remains available for repeated commands;
   each command runs in a fresh MicroVM and then removes that instance.
2. [Deploy your service](docs/deployment.md#production-start) with site credentials,
   durable data, resource pools and API/data ingress. Enable the native
   [egress gateway](docs/node-infra.md#native-egress) when Guests or Builds need
   outbound connectivity. The trial profile does not configure a public E2B service.
3. [Build an application template](docs/templates.md), keep its canonical ID, and
   [create, access and resume instances](docs/sandboxes.md).
4. Add [Registry + Router + two Nodes](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/cluster.md#minimum-cluster)
   when you need a shared entry and placement. Direct Group maintenance is enough
   to start; Placer and distributed cache are optional.

Workbench provides the development and validation environment. The
[Demo](test/demo/DEMO.md) and [release validation](test/QUICKSTART.md) exercise a
matching release; they are not prerequisites for operating the system container.

## Versions and support scope

Select one [Stable release](https://github.com/kuasar-sandbox/kuasar-sandbox/releases/latest)
or explicit Preview and retain its exact tag. An aggregate selects six component
release units: accelerator, connector, sandboxer, orchestrator, Runtime and
VMLinux. Their versions can differ. New aggregates provide native Linux x86_64
and aarch64 inputs; historical releases retain their published architecture set.

Stable/Preview describe release channels. Production deployments still qualify
capacity and configure TLS, durable storage, network policy and credentials.
The [architecture](docs/architecture.md) guide defines supported capabilities;
[performance](docs/performance.md) records measurements and their conditions.
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
