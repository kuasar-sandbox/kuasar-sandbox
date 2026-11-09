[English](node-infra.md) | [简体中文](node-infra_zh.md)

# Deploy and operate a node-infra system container

This guide deploys one kuasar-sandbox data-plane node from the **node-infra**
image and its matching deployment archive. It owns the host/container binding,
installation and instance lifecycle. Component repositories remain authoritative
for their native configurations and APIs. Platform node-pool routing, host
device allocation and external Cache/Store service deployment have their own
owners.

## Prerequisites and release inputs

Use a native amd64 or arm64 Linux host with systemd, cgroup v2, usable KVM/TUN,
network namespaces, BPF/TC and userfaultfd. Install rootful Podman, its Quadlet
generator and a supported OCI runtime. The deployment archive's `release.json`
records the selected support baseline; the shipped platform E2E exercises the
actual template. A successful systemd or generator probe alone is not VM or
recovery acceptance.

Acquire the same aggregate version of:

| Input | Use |
|---|---|
| `ghcr.io/kuasar-sandbox/node-infra:<version>` | Multi-architecture OCI image. Select and record the concrete platform manifest. |
| `node-infra-<version>-linux-<amd64|arm64>.oci.tar.gz` | Offline alternative for the same platform image, imported with `podman load`. |
| `node-infra-deploy-<version>.tar.gz` | Host tool, complete Quadlet template, runtime hooks, examples and this guide. |
| Matching workbench and `platform-<version>.tar.gz` | Repeatable acceptance environment and corresponding tests. |

Verify downloads through the existing GitHub Release asset size/digest fields.
`release.json` maps the image index/platform manifests, image configuration ID
and offline archives. The archive checksum, image configuration ID and manifest
digest identify different objects. Image component identities are also recorded
at `/usr/share/node-infra/image.json`.

Load or pull before service startup, verify the resulting platform/image
identity, and keep `Pull=never` in the instance template. For example:

```sh
sudo podman load --input node-infra-VERSION-linux-amd64.oci.tar.gz
# Or obtain the selected platform manifest from the registry:
sudo podman pull ghcr.io/kuasar-sandbox/node-infra@sha256:PLATFORM_MANIFEST_DIGEST
```

The image includes all node binaries and default boot assets. Conductor, Proxy,
Connector and their execution units form the normal startup chain; Telemetry
uses its native systemd enablement. Cache/Store endpoints come from the
node-level storage access layer. The deployment tool does not start those
external services.

## Prepare one instance's host resources

Prepare an independent namespace, for example `/run/netns/node-infra-node-a`:

| Input | State before startup | Use |
|---|---|---|
| `lo` | UP | Local communication. |
| `net0` | Present, UP, ingress address/routes configured | Conductor and Proxy ingress. |
| `net1` | Present, DOWN | Connector takes it into its internal switch namespace for transit. |
| Devices and mounts | Accessible to the selected runtime | KVM/TUN, cgroup delegation and component-created network resources. |
| Persistent directory | Private to this instance | Node database, writable disks and checkpoints. |

The host can move already allocated interfaces into the namespace and rename
them to the defaults. The delivery interface uses names and a namespace path;
it does not depend on whether a device is a VF, veth or another supported link.
Set the transit address/gateway through native Connector configuration. The
host owns ingress addressing/routes and restores the main namespace before
starting the instance after host reboot.

For native management services targeting loopback, the runtime assigns their
configured IPv4 VIPs to the corresponding management device and enables
`route_localnet`. This gives Proxy a local source address whose guest replies
return through the management plane. External management service addressing
remains the responsibility of its deployment.

Podman joins this namespace through `Network=ns:...`. Provide usable DNS and a
matching `/etc/resolv.conf`; joining a namespace does not create that file.
Make external storage endpoints and any file-backed artifacts reachable from
the instance. For UDS, mount the provider-owned parent directory so a recreated
socket is still visible. A storage endpoint does not make arbitrary host files
visible inside the container.

## Configure using native paths

The Quadlet file owns the image, namespace, devices, mounts, instance resource
limit and host service dependencies. Native files own component configuration;
there is no separate `node.yaml` schema.

| Service | Binary command | Base configuration |
|---|---|---|
| `node-conductor.service` | `node-ctl conductor serve` | `/etc/node-conductor/conductor.yaml` |
| `node-proxy.service` | `node-ctl proxy serve` | `/etc/node-proxy/proxy.yaml` |
| `node-telemetry.service` | `node-ctl telemetry serve` | `/etc/node-telemetry/telemetry.yaml` |
| `connector-vswitch.service` | `connector-ctl vswitch serve` | `/etc/connector-vswitch/vswitch.yaml` |

Each service starts with its image base file, followed by sorted
`/etc/<component>/conf.d/*.yaml`. The launcher passes one explicit
`--config PATH[:PATH...]` value to the native loader. Maps merge recursively;
later scalars and lists replace earlier values, and overlay null removes an
inherited key. Discovery happens only in the launcher. All delivered Connector
files use YAML. Manifest retains `/etc/node-infra/manifest.yaml` as its native
single-file entry.

Keep site policy in `50-platform.yaml` and instance bindings in
`90-instance.yaml`. Mount individual files or the `conf.d` directory, preserving
the image's base file. Customer Containerfile COPY uses these exact same paths.
Preserve `/opt/node-infra/bin/` sibling helpers and default
`/opt/node-infra/boot/vmlinux` and `sandbox-runtime.bundle` assets.

Supply the native domain/listeners, authentication/TLS configuration, encryption
key and Manifest endpoints for the deployment. The default internal API/data
ports are 3000/3001. Keep private configuration files root-owned and private.
The following are partial instance overlays, not replacements for base files:

```yaml
# /etc/node-conductor/conf.d/90-instance.yaml
api:
  domain: sandboxes.example.com
  listen: 0.0.0.0:3000
# Add the deployment's native encryption_key and API TLS/authentication settings.
sandbox:
  boot:
    overlay_diff_template: /var/lib/node-infra/overlay.ext4
```

```yaml
# /etc/connector-vswitch/conf.d/90-instance.yaml
transit_dev: net1
transit_dev_addr: "192.0.2.6/30:192.0.2.5"
```

For image cold starts, create a new sparse ext4 overlay template in the
instance's persistent data directory and reference its container path. For
example, with `DATA` set to the host directory bound to `/var/lib/node-infra`:

```sh
test ! -e "$DATA/overlay.ext4"
truncate -s 4G "$DATA/overlay.ext4"
mkfs.ext4 -F -q -b 4096 -O ^has_journal "$DATA/overlay.ext4"
```

Build workloads separately configure their native `builder.diff_template` and
source/publication dependencies. Named file artifacts use the native
`checkpoint.remote.ref_location_parent` mapping and corresponding directory
mounts. Boot asset discovery and snapshot compatibility remain native behavior.

### Resource pools and custom applications

Outer `CPUQuota`/`MemoryMax` bound the entire instance and all VM descendants.
Native resource pools describe the allocatable net budget; leave space for
management and Proxy processes. The packaged `examples/multi-pool/` includes
both Runner/Builder mappings and placement fragments. Set actual host CPU/NUMA
placement in those fragments when needed. Do not interpret host `MemTotal` as a
verified container budget.

The packaged `examples/extensions/` selects xconductor/xproxy with
`paths.conductor_executable` and `paths.proxy_executable`. Build them with the
SDK matching the base image, install root-owned executable files, and let
official node-ctl perform the native bootstrap. Private configuration is read
by the application; place environment settings on the actual component service
with a systemd `EnvironmentFile`. Versioned private file paths retain consistent
input across Proxy worker restart. Static `node-ctl config` checking does not
execute Configure/BindRuntime hooks; validate real custom startup and requests.

## Install and operate

From the extracted deployment package, copy `templates/node-infra.container`
to an instance file and replace its marked host binding values. Keep the
delivered permissions, namespace recovery mounts and lifecycle hooks. Set
instance limits under `[Service]`; prepare the referenced native configuration
files and directories before checking them.

```sh
sudo ./bin/node-infra-ctl install --name node-a --unit ./node-infra-node-a.container
sudo ./bin/node-infra-ctl check --name node-a --json
sudo systemctl daemon-reload
sudo systemctl start node-infra-node-a.service
sudo ./bin/node-infra-ctl status --name node-a --json

sudo journalctl -u node-infra-node-a.service
sudo podman exec node-infra-node-a journalctl -u node-conductor.service
sudo systemctl stop node-infra-node-a.service
```

Quadlet creates boot enablement from the source file's `[Install]` section.
`install` pins the host tools/hooks to a version below
`/opt/node-infra/releases/`. Installing a new package does not replace another
instance's pinned tools. Native service configurations are kept separately from
host instance records.

`check` validates startup inputs and unfinished recovery. `status` reports the
container, outer service, individual checks and `recovery.pending/error`;
`ready` is a separate result. Both return zero for their successful condition
and one for an unsuccessful check/not-ready state. A running container or a
Connector control socket alone does not establish available execution slots
or successful sandbox traffic. Admit workload only after actual node readiness
and the required business acceptance checks.

## Stop, recover and replace an instance

Full-instance stop ends its running VMs. The platform first stops new placement
and completes any required Pause/Export. The shutdown path stops new task
creation, stops and confirms all Runner/Builder consumers, then tears down
Connector from the instance's main namespace. Stopping only Conductor does not
stop every independent runner unit; restarting a component retains its native
recovery behavior.

Before starting PID 1, a short-lived native helper creates namespace pins in
the host's instance directory. The system container receives them with `rslave`
propagation, so its final unmount sweep cannot remove the host's pins. The host
retains these pins, bpffs access, applied image/configuration identity and the
recovery record. On normal shutdown and after a hard exit, the host hook
runs after the old container and Quadlet-generated removal have finished. It
uses the old image/configuration to recover the original transit device. A
failure retains the recovery inputs and blocks replacement. Use status and the
outer journal to identify the failed step, correct the stated input, then retry:

```sh
sudo ./bin/node-infra-ctl status --name node-a --json
sudo ./bin/node-infra-ctl recover --name node-a
sudo ./bin/node-infra-ctl check --name node-a --json
```

Recovery validates actual resource results. Do not replace a missing transit
with a same-named device or remove retained namespace pins to clear an error;
those objects identify the old resources being recovered. The tool manages
component-created resources, while the host keeps ownership of its main netns
and allocated devices.

For an update, acquire/verify target assets, preserve workload state as needed,
stop and clean with the old inputs, then install the target unit/configuration,
reload, start and verify. Explicit rollback uses a selected previous image and
matching configuration, subject to native state compatibility. Image replacement
does not transfer running memory or change snapshot/migration-token formats.

## Acceptance with published inputs

The matching platform package includes:

- `basic.node-infra-deploy.sh`: formal installation, real bare guest, ingress,
  native exec, transit, cgroup limits, memory and E-only pause/resume, normal stop.
- `basic.node-infra-config.sh`: layered native configuration, SDK extensions and
  private service input, worker rebuild, two actual execution pools and partial
  startup failure cleanup.
- `basic.node-infra-lifecycle.sh`: two live instances, PID 1 and conmon hard
  exit/recovery, changed configuration, cleanup conflict/retry with missing BPF
  metadata, restart and stop isolation.

Preparation takes the matching deployment package and native OCI archive;
execution loads it into Podman and uses the delivered tool/template. The guest
and extension helpers are prebuilt in the normal platform helper package.
Tests create only their own namespaces/devices/state; a failed cleanup retains
its recovery inputs and reports failure. They do not start a substitute
Conductor/Proxy beside the system container.

Use the public E2E runner's `--include 'basic.node-infra-*.sh'` selection in
workbench system mode or the target host. Missing inputs/capabilities fail
explicitly. Report native architecture, runtime/kernel versions and actual
passed/failed/skipped tests; each supported architecture needs its own KVM
evidence. See the source [test quickstart](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/main/test/QUICKSTART.md)
for preparation/run commands and [native App contracts](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/extensions.md)
for application integration.
