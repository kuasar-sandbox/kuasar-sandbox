[English](README-deployment.md) | [简体中文](README-deployment_zh.md)

# Kuasar Release deployment

`kuasar_deploy.py` prepares and verifies an aggregate Kuasar Sandbox Release on a
single supported test host.

## Supported host

- Linux on x86_64
- systemd with cgroup v2
- readable and writable `/dev/kvm`
- glibc 2.38 or newer
- passwordless `sudo` for the invoking user
- a running Docker daemon
- HTTPS access to public GitHub Release assets with a trusted CA chain
- no existing Kuasar demo deployment using the same network resources

Run the tool as your regular user, not with `sudo`. It requests passwordless
`sudo` itself for operations that require elevated privileges.

## Quick start

From the repository root:

```bash
./release/kuasar_deploy.py quick-start --version release-v0.1.5
```

The command installs missing documented dependencies, validates the host,
downloads the specified Release, verifies its checksums, extracts its binaries,
starts the demo services, and runs the official end-to-end verification.

A successful run exits with status 0 and ends with a result summary such as:

```text
=== KUASAR DEPLOYMENT RESULT ===
Command: quick-start
Release: release-v0.1.5
Result: SUCCEEDED
```

A failed run exits with a non-zero status and reports `Result: FAILED` together
with the reason. Run only the environment validation with:

```bash
./release/kuasar_deploy.py check
```

To stop a prepared demo deployment:

```bash
./release/kuasar_deploy.py stop --release-dir /path/to/extracted-release
```

## Anonymous downloads

Public Release downloads do not require GitHub CLI, a GitHub account, or a token.
The tool downloads SHA256SUMS over verified HTTPS, obtains archive names from it,
and verifies each archive before extraction. Configure the host trust store (or
SSL_CERT_FILE) for a corporate proxy CA; TLS verification remains enabled.
Verified cached archives are reused after a download failure. Retry the same
command. An existing installation directory is still protected from overwrite.
Private Release repositories are not supported by this anonymous path.


## Unit isolation compatibility

`release-v0.1.5` uses unique runner and builder unit names for every demo run.
The deployer reports this as `Unit isolation: per-run`. Some future Releases may
support configurable names through `DEMO_UNIT_PREFIX`; the deployer passes
`--unit-prefix` only to those Releases. It fails before deployment if a selected
Release supports neither form of isolation, and it rejects a custom prefix when
the Release supports only per-run names. In particular, `release-v0.1.2` does
not support this option and is not a supported default for this tool.

## Verification dependency versions

The verification environment is isolated in `.venv` and installs the exact SDK
requirements shipped in the verified Release at `test/demo/requirements.txt`.
This keeps the SDK compatible with the selected Release without modifying the
host Python environment.
