# Security Policy

[简体中文](SECURITY.md) · [Project overview](README_EN.md)

Kuasar Sandbox executes potentially untrusted workloads and spans several security boundaries: API identity, host and guest isolation, snapshot and image data, storage/cache domains, network identity and policy, node resource control, and release supply chain. Please report suspected vulnerabilities privately.

## Supported versions

Security fixes are normally coordinated for:

- the current stable aggregate release;
- supported preview releases when the issue is present in the selected source set;
- the current `main` branches of the affected project or component repositories.

The response team may ask you to confirm the exact aggregate release, component tag or commit, guest runtime/kernel version, deployment mode, storage path, and network configuration.

## Report a vulnerability privately

Use GitHub private vulnerability reporting in the [`kuasar-sandbox/kuasar-sandbox`](https://github.com/kuasar-sandbox/kuasar-sandbox) repository:

1. Open the repository's **Security** tab.
2. Select **Report a vulnerability**.
3. Create a private report with the information below.

Do **not** disclose a vulnerability through a public issue, pull request, discussion, commit, workflow log, or attached archive. Do not include real customer data, production credentials, private keys, access tokens, internal endpoints, or unredacted sensitive logs.

The project repository is the common intake point for cross-component reports. Maintainers will route the report privately to the affected component owners.

## Helpful report contents

Please include, where applicable:

- affected aggregate release and component versions/commits;
- host architecture, Linux kernel, VMM, guest kernel, and runtime versions;
- single-node or cluster deployment mode;
- local, shared-file, or manifest/object-storage data path;
- network mode and whether an external policy gateway is involved;
- required attacker position and privileges;
- minimal reproduction steps or proof of concept;
- observable impact and affected security boundary;
- sanitized logs, traces, packet excerpts, or stack traces;
- whether the issue is already being exploited or publicly discussed;
- suggested mitigations, when known.

Use generated test data and removable credentials. If a report requires a large or unusually sensitive artifact, describe it first and wait for a private transfer method.

## What to expect

Maintainers will normally:

1. acknowledge the private report;
2. reproduce and classify the issue;
3. identify affected repositories, releases, and deployment modes;
4. coordinate fixes and release sequencing across components when required;
5. agree on disclosure timing and credit with the reporter;
6. publish advisories and patched releases when appropriate.

Response time depends on severity, reproducibility, cross-component impact, and release complexity. Please avoid public disclosure until a coordinated fix or mitigation is available.

## Scope examples

Security reports may include, but are not limited to:

- escape or host compromise from a guest workload;
- cross-sandbox or cross-tenant data, credential, network, or resource-boundary violations;
- authentication, token-scope, or authorization bypass;
- unsafe snapshot, image, manifest, cache, or local-artifact handling;
- encryption, integrity, key-lifecycle, or security-domain failures;
- sandbox identity spoofing or policy-gateway bypass;
- privileged CI, release, artifact, cache, or dependency-supply-chain compromise;
- unsafe parsing or path traversal in host-facing inputs;
- denial of service that bypasses documented admission and isolation boundaries.

General hardening suggestions, configuration questions, and non-security bugs can use normal issues or discussions after removing sensitive information.

## Public disclosure and credit

After remediation, the project may publish a GitHub Security Advisory and release notes describing affected versions, impact, mitigation, and fixed versions. Reporter credit is included with permission.

## Repository-specific policies

A component repository may provide additional scope or contact instructions. Repository-specific policies take precedence for that repository, while the project repository remains the fallback private intake point for cross-component issues.