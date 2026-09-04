# Kuasar Sandbox Quick Start

[简体中文](quickstart.md) · [Project overview](../README_EN.md)

This guide provides the shortest release-based path for evaluating Kuasar Sandbox on one Linux node. It uses the prebuilt artifacts and demo scripts shipped by one aggregate release; it is not the full release-validation procedure.

## What this validates

The flow exercises:

1. downloading and verifying one coordinated aggregate release;
2. preparing a local single-node environment;
3. building or importing a sandbox template;
4. creating a real KVM MicroVM through the E2B-compatible API;
5. executing a command in the guest;
6. pausing and reconnecting to the same logical sandbox;
7. terminating the sandbox and cleaning up the demo environment.

For exhaustive component and platform gates, use the [aggregate release validation guide](../test/QUICKSTART.md). For the complete demo, including networking and migration scenarios, see [DEMO.md](../test/demo/DEMO.md).

## Prerequisites

Use a dedicated Linux test machine with:

- an x86-64 CPU with hardware virtualization enabled;
- `/dev/kvm` available to the test user or through `sudo`;
- systemd and cgroup v2;
- a recent Linux kernel compatible with the selected release;
- `bash`, `curl`, `tar`, `sha256sum`, `python3`, and `sudo`;
- Docker or another OCI-compatible engine when the template preparation path needs to build or publish an image;
- enough local disk and memory for the selected guest image and snapshot.

The demo configures privileged host resources. Run it on an isolated machine, not on a workstation or production node that already has conflicting bridges, TAP devices, routes, ports, or services.

Verify the essential host capabilities:

```bash
uname -m
test "$(uname -m)" = x86_64
test -c /dev/kvm
systemctl --version
stat -fc %T /sys/fs/cgroup | grep -qx cgroup2fs
```

## 1. Download one aggregate release

Choose an existing aggregate release from the project repository. Stable releases use names such as `release-v0.1.2`; coordinated development builds use `release-vX.Y.Z-preview.YYYYMMDD`.

```bash
export KUASAR_RELEASE=release-v0.1.2
export KUASAR_RELEASE_DIR="$PWD/kuasar-${KUASAR_RELEASE}"
mkdir -p "$KUASAR_RELEASE_DIR/download" "$KUASAR_RELEASE_DIR/root"
```

Download **all** archives and `SHA256SUMS` from the same aggregate release page. With GitHub CLI:

```bash
gh release download "$KUASAR_RELEASE" \
  --repo kuasar-sandbox/kuasar-sandbox \
  --dir "$KUASAR_RELEASE_DIR/download"
```

The aggregate release normally includes:

- one platform archive containing project docs, deployment files, demos, and aggregate tests;
- one archive for each of `accelerator`, `connector`, `sandboxer`, and `orchestrator`;
- one Runtime archive and one VMLinux archive from `guest-runtime`;
- a release-wide `SHA256SUMS` file.

Do not combine assets from different stable versions or preview dates.

## 2. Verify and extract the release

```bash
cd "$KUASAR_RELEASE_DIR/download"
sha256sum --quiet -c SHA256SUMS

for archive in ./*.tar.gz; do
  tar -xzf "$archive" -C "$KUASAR_RELEASE_DIR/root"
done

cd "$KUASAR_RELEASE_DIR/root"
find bin deploy docs test -maxdepth 2 -type f | head -80
```

Stop if checksum verification fails. Do not run partially downloaded or mixed-version artifacts.

## 3. Review the demo configuration

The release includes the authoritative demo scripts and detailed instructions:

```bash
less test/demo/DEMO.md
```

Before running the scripts, review:

- host interfaces and routes the demo will create;
- ports used by the local registry, object-storage substitute, node service, and proxy;
- working and persistent directories;
- whether the selected path uses local files, shared storage, or an object-storage-compatible backend;
- cleanup commands.

Demo certificates, credentials, and local endpoints are test-only values. Replace them in a production deployment.

## 4. Prepare the single-node demo

Run the preparation script shipped by the same release:

```bash
sudo -E bash test/demo/demo_prep.sh
```

The script prepares the release-owned local services and artifacts required by the E2B-compatible scenario. Follow any environment variables or prerequisite commands printed by the script; the exact set can evolve with aggregate releases.

Confirm that the expected services are healthy before continuing. Use the service and health commands documented in `test/demo/DEMO.md` for the selected release rather than copying commands from another version.

## 5. Run the E2B-compatible lifecycle demo

Install the supported E2B Python SDK in an isolated environment:

```bash
python3 -m venv "$KUASAR_RELEASE_DIR/venv"
. "$KUASAR_RELEASE_DIR/venv/bin/activate"
python -m pip install --upgrade pip
python -m pip install e2b
```

Run the release-provided scenario:

```bash
sudo -E env PATH="$PATH" bash test/demo/demo_e2b.sh
```

The scenario should build or import a template, create a sandbox, execute a guest command, pause the sandbox, reconnect to the same logical sandbox, execute another command, and terminate it.

The equivalent SDK shape is:

```python
from e2b import Sandbox

# Configure the API endpoint and credentials exactly as printed by the
# release-provided demo preparation step.
sandbox = Sandbox.create("<template-id>")

result = sandbox.commands.run("printf 'hello from Kuasar Sandbox\\n'")
print(result.stdout)

sandbox_id = sandbox.sandbox_id
sandbox.pause()

# Reconnect using the API supported by the installed E2B SDK release.
resumed = Sandbox.connect(sandbox_id)
print(resumed.commands.run("uname -a").stdout)
resumed.kill()
```

SDK method names can change between E2B SDK releases. The release-provided `demo_e2b.sh` is the executable source of truth for the SDK version used by the selected Kuasar Sandbox release.

## 6. Validate the result

A successful run demonstrates more than process creation. Check that:

- a real KVM MicroVM was created;
- the command output came from the guest;
- the sandbox kept the same logical identity across pause and reconnect;
- guest process/filesystem state expected by the demo survived the lifecycle;
- termination removed the release-owned sandbox unit and runtime resources;
- the demo reports no failed lifecycle step.

For production qualification, also run the aggregate prebuilt E2E and the deployment-specific storage, network, resource-pressure, recovery, and upgrade checks.

## 7. Clean up

Use the cleanup path documented and shipped with the selected release. A typical release provides cleanup through the demo scripts or the commands in `test/demo/DEMO.md`.

After cleanup, confirm that release-owned resources are gone:

```bash
systemctl list-units --all | grep -i kuasar || true
ip link show | grep -E 'tap|kuasar' || true
ps -ef | grep -E 'node-ctl|cluster-ctl|sandboxer|connector|store|cache' | grep -v grep || true
```

Do not delete similarly named resources that were not created by this demo.

## Next steps

- [Architecture overview](kuasar-sandbox.md)
- [Deployment guide](deployment.md)
- [Complete demo](../test/demo/DEMO.md)
- [Aggregate release validation](../test/QUICKSTART.md)
- [Component repositories](../README_EN.md#repositories)
- [Security policy](../SECURITY.md)
- [Contribution guide](../CONTRIBUTING.md)

For questions and design discussion, use the project repository's GitHub Discussions. Report vulnerabilities only through the private process described in the Security Policy.