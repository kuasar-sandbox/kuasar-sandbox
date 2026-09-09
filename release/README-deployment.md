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
- an authenticated GitHub CLI (`gh auth login`)
- no existing Kuasar demo deployment using the same network resources

Run the tool as your regular user, not with `sudo`. It requests passwordless
`sudo` itself for operations that require elevated privileges.

## Quick start

From the repository root:

```bash
./release/kuasar_deploy.py quick-start --version release-v0.1.2
```

The command installs missing documented dependencies, validates the host,
downloads the specified Release, verifies its checksums, extracts its binaries,
starts the demo services, and runs the official end-to-end verification.

A successful run exits with status 0 and ends with a result summary such as:

```text
=== KUASAR DEPLOYMENT RESULT ===
Command: quick-start
Release: release-v0.1.2
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
