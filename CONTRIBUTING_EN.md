# Contributing to Kuasar Sandbox

[简体中文](CONTRIBUTING.md) · [Project overview](README_EN.md)

Thank you for contributing to Kuasar Sandbox. The project uses one system repository and five loosely coupled component repositories. Choose the smallest repository that owns the behavior you want to change.

## Choose the right repository

| Repository | Use it for |
| --- | --- |
| `kuasar-sandbox/kuasar-sandbox` | System-level documentation, aggregate releases, cross-component BMS/E2E, demos, and changes that coordinate several components |
| `kuasar-sandbox/orchestrator` | E2B-compatible node APIs, node lifecycle orchestration, resource admission, proxying, and Registry/Router/Placer cluster control |
| `kuasar-sandbox/sandboxer` | MicroVM lifecycle, snapshot/restore, guest control, block devices, memory loading, and native execution |
| `kuasar-sandbox/accelerator` | Data and sparse-artifact APIs, encryption, manifests, stores, caches, OCI pull, and image flattening |
| `kuasar-sandbox/connector` | eBPF networking, TAP attachment, sandbox isolation/identity, and external policy-gateway integration |
| `kuasar-sandbox/guest-runtime` | Guest kernel, runtime image, image construction, and Runtime/VMLinux release inputs |

Ordinary changes should stay in one component repository. Do not move a component implementation into the project repository merely to simplify a pull request.

## Development workspace

For cross-component work, clone the six repositories as siblings:

```text
workspace/
├── kuasar-sandbox/
├── orchestrator/
├── sandboxer/
├── accelerator/
├── connector/
└── guest-runtime/
```

Each component documents its standalone build and test commands. The project repository documents aggregate build, demo, BMS, and release-validation entry points.

## Before opening a pull request

1. Search existing issues and pull requests.
2. Describe the user or operator problem before proposing an implementation.
3. Keep the change focused; separate unrelated cleanup and feature work.
4. Update user-facing documentation when behavior, configuration, compatibility, or deployment requirements change.
5. Run the component's local unit/static checks.
6. Run privileged or cross-component tests only in the documented environment.
7. Do not commit credentials, private certificates, internal endpoints, customer data, production logs, or proprietary dependencies.
8. Review `git diff --check` and the final pull-request diff.

## Cross-repository changes

A protocol, exported API, release contract, or end-to-end change may require companion pull requests.

In every involved pull request:

- link all companion pull requests;
- identify the exact target branch and head commit of each companion;
- state the required merge order, if any;
- explain compatibility during partial rollout;
- record the exact source set used by BMS/E2E;
- avoid merging one side while required companions are not ready.

A component-only refactor that preserves exported behavior should not be expanded into a multi-repository change.

## Tests and privileged environments

Local contributors should run the checks that do not require privileged infrastructure. Tests involving KVM, systemd units, cgroup management, TAP/netns/eBPF, kernel or runtime builds, object-storage credentials, or multi-node topology may run through the project's controlled BMS.

A skipped privileged test is not evidence that the privileged path passed. Pull-request descriptions must distinguish executed checks, skipped checks, and checks delegated to BMS.

External fork code must never receive release credentials, organization-private repository access, production infrastructure credentials, or control-plane privileges. Follow repository-specific instructions before requesting privileged BMS execution.

## Commit and pull-request quality

- Use clear, imperative commit messages; Conventional Commit prefixes are encouraged where the repository already uses them.
- Explain rationale and observable behavior, not only code mechanics.
- Include validation and known limits.
- Preserve license and attribution headers for third-party or generated material.
- Resolve review threads and required checks before merge.
- Do not rewrite or replace published stable tags or release assets.

## Documentation and language

The English README is the default international entry point. Chinese companion documents may be provided as `*_zh.md` or the established repository-specific paired name. Keep reciprocal language links near the top and keep terminology consistent across repositories.

Full translation of every design document is not required for a focused code contribution. Changes to public behavior, configuration, build instructions, security boundaries, or release contracts must update the relevant English entry point.

## Security

Do not open a public issue for a vulnerability. Follow the [English Security Policy](SECURITY_EN.md) or the repository's Security tab and use private vulnerability reporting.

## License

By submitting a contribution, you agree that project-owned changes are provided under the repository's Apache License 2.0 terms unless the file clearly belongs to a third-party license scope. Preserve upstream copyright, license, source, and notice obligations for Linux, eBPF, Cloud Hypervisor, generated code, runtime packages, and other third-party content.