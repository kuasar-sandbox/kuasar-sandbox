[English](SECURITY.md) | [简体中文](SECURITY_zh.md)

# Security Policy

Kuasar Sandbox receives system-level and cross-component security reports through the project repository. Reports are coordinated privately and routed to the maintainers of `orchestrator`, `sandboxer`, `accelerator`, `connector`, or `guest-runtime` as needed.

## Supported versions

The current supported stable aggregate release is [`release-v0.1.2`](https://github.com/kuasar-sandbox/kuasar-sandbox/releases/tag/release-v0.1.2).

Please report vulnerabilities that affect the current Stable release, the latest published aggregate Preview, or the current `main` branch. Include the exact aggregate and component versions that are affected. Reports about older Preview releases are evaluated according to whether the issue still affects the current Stable release, the latest Preview, or `main`.

| Version range | Status |
| --- | --- |
| `release-v0.1.2` | Current supported Stable release |
| Latest published aggregate Preview | Prerelease reports accepted |
| Current `main` | Source reports accepted |

Preview is a GitHub prerelease channel. The production readiness of the system and the stability label of a public release channel are separate concerns.

## Report a vulnerability privately

Use **Report a vulnerability** on this repository's Security page to submit a [private vulnerability report](https://github.com/kuasar-sandbox/kuasar-sandbox/security/advisories/new).

Do not disclose an unpatched vulnerability in a public issue, discussion, pull request, or external post.

A useful report includes:

- the affected aggregate release tag;
- the affected component and exact component version;
- deployment mode, CPU architecture, storage path, and relevant network topology;
- prerequisites, reproduction steps, and required privileges;
- the observed impact and the expected security boundary;
- sanitized logs, error output, or a minimal reproduction.

Do not submit real credentials, keys, customer data, unpublished images, internal host addresses, or other sensitive information. Use minimal and revocable test data instead.

## What happens after a report

Maintainers will acknowledge the report in the private advisory, review the reproduction conditions and impact, and identify the responsible component or components. For cross-repository issues, the project repository coordinates the affected component maintainers without publishing the vulnerability details.

The response may include validating a fix, preparing component and aggregate releases, and coordinating disclosure timing and content with the reporter. We do not promise a fixed response or remediation time before triage; complexity, affected versions, and cross-component scope can change the coordination required.

## Scope

Security reports may cover the MicroVM boundary, host/guest control paths, identity and scoped credentials, snapshot and image data protection, storage and cache trust boundaries, sandbox networking, node resource isolation, build and release supply chains, and cross-component integration.

General bugs, feature requests, deployment questions, and performance reports should use the appropriate public issue or discussion after removing sensitive information.
