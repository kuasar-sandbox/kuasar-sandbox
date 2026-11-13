[English](node-infra.md) | [简体中文](node-infra_zh.md)

# Deploy and operate a node-infra system container

This guide deploys one kuasar-sandbox data-plane node from the **node-infra**
image and its matching deployment archive. It owns the host/container binding,
installation and instance lifecycle. Component repositories remain authoritative
for their native configurations and APIs. Platform node-pool routing, host
device allocation and external Cache/Store service deployment have their own
owners.

## Prerequisites and release inputs

Use a native amd64 or arm64 Linux host with cgroup v2, usable KVM/TUN,
network namespaces, BPF/TC and userfaultfd. Production uses host systemd with
rootful Podman/Quadlet. Development can use rootful Docker directly, including
the cgroupfs driver; host node-infra units and Podman are not prerequisites for
that path. The same image runs systemd as container PID 1 in both cases. The deployment archive's `release.json`
records the selected support baseline; the shipped platform E2E exercises the
actual template. A successful systemd or generator probe alone is not VM or
recovery acceptance.

Acquire the same aggregate version of:

| Input | Use |
|---|---|
| `ghcr.io/kuasar-sandbox/node-infra:<version>` | Multi-architecture OCI image. Select and record the concrete platform manifest. |
| `node-infra-<version>-linux-<amd64|arm64>.oci.tar.gz` | Offline alternative for the same platform image, imported with `podman load`. |
| `node-infra-<version>-linux-<amd64|arm64>.docker.tar.gz` | Docker-compatible offline archive from the same OCI image; import with `docker load`. Available with `node-infra-v2`. |
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

## Run your first Guest with Docker

On a supported development host, set `IMAGE` to the GHCR reference for a release that includes Quick Start. For offline use, load that release's Docker archive, set `IMAGE` to its loaded image ID or `localhost/node-infra:<version>`, and add `--pull=never` to `docker run`.

```sh
docker run -d --tty --name ks-dev --privileged \
  --cgroupns=private --cpus=4 --memory=6g --memory-swap=6g \
  --tmpfs /run --tmpfs /run/lock --tmpfs /tmp:exec --stop-timeout=420 \
  --network=bridge -v ks-dev-data:/var/lib/node-infra "$IMAGE" quickstart
docker exec ks-dev node-infra-run -- /bin/sh -c 'printf "hello from guest\n"'
```

`node-infra-run` waits for initialization and an idle runner, executes the supplied argument vector in a fresh MicroVM, returns its output and exit code, and deletes that Guest. It uses the Conductor and Proxy inside the node. The example publishes no host ports and needs no Guest internet access. The 4 CPU and 6 GiB settings are an example for one node, not a minimum sizing rule.

Quick Start initializes an empty private volume once. It uses instance-specific credentials and the shipped native Linux shell filesystem to produce an offline Image Manifest Bundle. Disk-only exports and memory snapshots default to Manifest Bundles with native `@manifest` selectors. Keep the volume and its referenced base Bundles together for recovery. Use `docker stop ks-dev` for orderly shutdown and `docker start ks-dev` to reuse it. Use separate volumes for simultaneous nodes. For site-supplied native configuration, use the deployment path below.

## Prepare the default host deployment

For one instance on a host without a dedicated transit exit, keep the delivered
`Network=host`. No primary namespace, specially named ingress interface or
transit device is required. Conductor and Proxy listen on the native configured
addresses/ports using the host's existing network. Keep loopback UP and provide
usable routes, DNS, KVM/TUN, cgroup delegation and a private persistent directory.

The tool pins the local runner's actual network namespace in the instance's
private runtime directory; it does not assume that `/proc/1/ns/net` identifies
that context. Preflight verifies Podman's selected namespace, and startup/status
verify the main container against the retained handle. Run the tool and rootful
Podman in the same local host context.

The default planes are the primary network (Conductor API and Proxy ingress),
`sw0-switch` (vSwitch), and `mgmt` (management). Connector defaults to
`mgmt_extracts: ["mgmt:mgmt0:169.254.169.254/32"]` and keeps
`mgmt_services: ["169.254.169.254:80:127.0.0.1:19254"]`. Proxy and Telemetry use
native `proxy_netns: mgmt`; Telemetry enablement remains explicit.

Proxy's master opens the data listener in the primary network. Its workers,
backend connections and MMDS listener use MGMT, while Conductor supplies MMDS
policy over the native control channel. Customer xproxy worker connections use
that worker namespace; connections opened by a master extension use the master's
primary namespace. Do not put the whole Proxy service in `NetworkNamespacePath`.

The runtime assigns local service VIPs and per-interface `route_localnet` only
on verified instance management peers. Connector installs device-scoped floating
return routes. Preflight checks each actual management plane, including borrowed
namespaces; a pre-existing egress default route is allowed. The default layout
leaves the host's existing metadata VIP, routes and listeners untouched.
Primary API/data listener conflicts still fail. Explicit older native layouts
remain explicit; changing defaults requires a maintenance rebuild, not hot
migration or automatic rewriting of configuration files.

Provide reachable external Cache/Store endpoints and any file-backed artifacts.
For UDS, mount the provider-owned parent directory so a recreated socket remains
visible. Storage endpoints do not make arbitrary host files visible inside the
container. Host networking does not expose them automatically either.

The default Connector configuration omits transit. Proxy ingress, guest Exec and
MMDS/management communication remain available; ordinary guest internet egress
is not supplied. This deployment does not install NAT or a GENEVE gateway.

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

Set `Image` to `sha256:<64-hex-configuration-ID>` or a digest-qualified registry
reference. Include the `sha256:` prefix even when `podman image inspect` returns
the configuration ID without it. For a customer image with baked configuration,
remove the corresponding configuration `Volume` entries or mount only the
individual site files: mounting a whole `conf.d` directory hides its image
contents. Retain the instance's data and recovery mounts.

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

## Direct Docker development

Use the same immutable image and native configuration paths. The short entrypoint
recognizes Docker/Podman, verifies private cgroup v2 delegation, prepares private
bpffs when absent, protects global `/proc/sys` and `/sys`, and execs `/sbin/init`.
It does not bind the writable host cgroup tree or install a supervisor. This is
a trusted administrator privileged system container, not a tenant boundary.

With `IMAGE`, `CONFIG_ROOT` and `DATA_ROOT` set to the selected image and prepared
absolute paths (including all four `conf.d` directories and `manifest.yaml`):

```sh
docker load --input node-infra-VERSION-linux-amd64.docker.tar.gz
docker run -d --tty --name node-infra-dev --pull=never --privileged \
  --cgroupns=private --network=host --cpus=4 --memory=6g --memory-swap=6g \
  --tmpfs /run --tmpfs /run/lock --tmpfs /tmp:exec --stop-timeout=420 \
  -v "$CONFIG_ROOT/node-conductor/conf.d:/etc/node-conductor/conf.d:ro" \
  -v "$CONFIG_ROOT/node-proxy/conf.d:/etc/node-proxy/conf.d:ro" \
  -v "$CONFIG_ROOT/node-telemetry/conf.d:/etc/node-telemetry/conf.d:ro" \
  -v "$CONFIG_ROOT/connector-vswitch/conf.d:/etc/connector-vswitch/conf.d:ro" \
  -v "$CONFIG_ROOT/manifest.yaml:/etc/node-infra/manifest.yaml:ro" \
  -v "$DATA_ROOT:/var/lib/node-infra:rw" "$IMAGE"
docker exec node-infra-dev /usr/libexec/node-infra/node-infra-runtime health
docker logs node-infra-dev
docker exec node-infra-dev journalctl --no-pager
docker stop node-infra-dev
```

For bridge, replace `--network=host` with `--network=bridge` and publish
`-p 127.0.0.1:23000:3000 -p 127.0.0.1:23001:3001`. Three instances use distinct
names/data/config directories and port pairs `23000/23001`, `23010/23011`,
`23020/23021`. Container bridge connectivity does not provide sandbox NAT.
Outer limits cover management, pools and VM descendants; native pool budgets
must leave management headroom, with native CPU/NUMA placement unchanged.

The image stop signal is `SIGRTMIN+3`. The 420-second runner budget covers the
ordered inner stop budgets; the production outer unit additionally allows its
recovery hook. The TTY supplies systemd’s console to `docker logs`. With privileged PID namespaces,
a completed halt may report exit 130; verify the network cleanup completion log
and resource results, not just the exit code. A forced stop is a different result
from normal internal cleanup.
`node-infra-ctl health --runtime docker --container node-infra-dev` is a thin
adapter to the internal health entry; `describe` and `check` work the same way.
No `node-infra-ctl install` is required for this Docker path.

## Explicit external management and egress

Bind only each referenced nsfs handle, read-only, at
`/run/node-infra/netns-inputs/<name>`. Do not expose the host's entire `/run/netns`.
For example, prepare the external `node-a-egress` namespace's exit device,
addresses, default route, forwarding and SNAT on the host, then add:

```sh
# Additional docker run argument; --mount fails if the source does not exist.
--mount type=bind,source=/run/netns/node-a-egress,target=/run/node-infra/netns-inputs/egress,readonly
```

The equivalent Quadlet binding is
`Volume=/run/netns/node-a-egress:/run/node-infra/netns-inputs/egress:ro`.
Use the native Connector overlay:

```yaml
mgmt_extracts:
  - "mgmt:mgmt0:169.254.169.254/32"
  - "egress:egress0:0.0.0.0/0"
```

Arrays replace the entire inherited list: keep the management item, with the
catch-all last. Only referenced names are imported. A supplied placeholder,
wrong namespace type or changed identity fails; unprovided named management
planes are created privately. External switch/port namespace adoption is not
supported. The applied record ties borrowed inputs to boot/generation and their
actual handles. The instance releases only its local reference and Connector's
own peers/return routes. Existing external devices, addresses, default routes,
SNAT and namespace handles remain the caller's property.

Customer networking uses native oneshot units and service drop-ins. See
`examples/network-extension/` in the deployment package. Its setup/teardown
owns only its named nft table, runs between network preparation and Connector,
and retains the base Connector `ExecStartPost` readiness step. An appended
post-start failure fails Connector and its startup dependents. The official
image includes nftables, with the distribution-wide flush service masked; derived images must include any other required tools
at build time. Never download packages during runtime setup.

## Optional namespace isolation and transit

Both choices use the same template/tool and are independent: host or explicit
namespace, each with or without transit. To isolate an instance, replace only
`Network=host` with `Network=ns:/run/netns/node-a`. Before `check`, prepare that
retained nsfs mount, bring its loopback UP and configure the required ingress
addresses/routes on any suitable interface name, for example `ingress0`. The
path must remain absolute, canonical and unlinked. Provide suitable DNS and
`/etc/resolv.conf`; joining a namespace does not create them. After reboot,
restore that external namespace before starting the instance.

To enable a dedicated transit device in either primary-network mode, explicitly
provide a native fragment (also shipped as `examples/transit/90-transit.yaml`):

```yaml
# /etc/connector-vswitch/conf.d/90-transit.yaml
transit_dev: uplink0
transit_dev_addr: "192.0.2.6/30:192.0.2.5"
transit_dev_mtu: auto
```

The selected device must exist in the primary namespace, have a stable MAC and
be DOWN before handoff. Never select an UP interface carrying host traffic.
Connector moves it into the internal switch namespace and returns the same
device DOWN during cleanup. Missing/empty transit performs no handoff or return
and records no fictitious device identity. The host owns device allocation;
this package does not manage VFs or deploy transit gateways. The same fragment
works through customer image COPY and runtime mounts.

For Docker with borrowed transit, explicitly prepare retained handles before the
first start. This optional path accepts a complete, never-started container ID
from the local rootful Docker daemon. It uses native host networking; ordinary
bridge development remains available without borrowed transit. Keep the normal
image entrypoint, no automatic restart/auto-remove, and the three recovery binds.
Use a dedicated native cgroup parent so recovery can prove that all old consumers
have ended even when the caller dies immediately after `docker start`:

```sh
name=node-a
run=/run/node-infra/instances/$name
state=/var/lib/node-infra/instances/$name
sudo install -d -m 700 "$run/netns" "$run/bpf" "$state/recovery" "$state/data"
parent=/node-infra-$name
if [ "$(docker info --format '{{.CgroupDriver}}')" = systemd ]; then
  suffix=$(printf %s "$name" | sha256sum | cut -c1-16)
  parent=nodeinfra${suffix}.slice
fi
# Add the same native configuration/fixture bindings used by the direct example.
id=$(docker create --tty --name node-infra-$name --pull=never --privileged \
  --network=host --cgroupns=private --cgroup-parent="$parent" \
  --cpus=4 --memory=6g --memory-swap=6g --stop-timeout=420 \
  --tmpfs=/run --tmpfs=/run/lock --tmpfs=/tmp:exec \
  -v "$CONFIG/node-conductor/conf.d:/etc/node-conductor/conf.d:ro" \
  -v "$CONFIG/node-proxy/conf.d:/etc/node-proxy/conf.d:ro" \
  -v "$CONFIG/connector-vswitch/conf.d:/etc/connector-vswitch/conf.d:ro" \
  -v "$CONFIG/manifest.yaml:/etc/node-infra/manifest.yaml:ro" \
  -v "$state/data:/var/lib/node-infra:rw" \
  -v "$state/recovery:/run/node-infra/recovery:rw" \
  -v "$run/netns:/run/netns:rw,rslave" -v "$run/bpf:/sys/fs/bpf:rw" "$IMAGE_ID")
sudo ./bin/node-infra-ctl prepare --runtime docker --name "$name" --container "$id"
docker start "$id"
sudo ./bin/node-infra-ctl started --runtime docker --name "$name"
# After normal stop or abnormal exit, use the recorded old generation.
docker stop "$id"
sudo ./bin/node-infra-ctl recover --runtime docker --name "$name"
```

The tool records the exact container/image, local daemon, dedicated cgroup,
namespace generation and pinned cleanup implementation. Recovery removes the
old stopped container by ID before releasing handles; a reused name cannot
redirect it. An interrupted daemon-side helper create whose outcome is still
unknown retains its journal and handles and reports that uncertainty. Do not
remove the pins or edit the journal to bypass it; inspect the recorded helper
and retry recovery if the helper appears after the daemon completes the operation.
If the helper remains absent, recovery can continue to refuse within the same
boot: an absent inspection result does not prove that a pending create request
has ended. Do not delete the journal or pins manually. Automatic retirement of
an absent helper intent requires a different boot and disappearance of the
entire old runtime directory.

## Stop, recover and replace an instance

Full-instance stop ends its running VMs. The platform first stops new placement
and completes any required Pause/Export. The shutdown path stops new task
creation, stops and confirms all Runner/Builder consumers, then tears down
Connector from the instance's main namespace. Stopping only Conductor does not
stop every independent runner unit; restarting a component retains its native
recovery behavior.

`node-infra-network.service` owns setup and `ExecStopPost` cleanup, including
partial setup failure. Connector's service owns its process and base readiness
step. The independent drain unit stops the applied native Runner/Builder pools,
including custom templates, and waits for their actual cgroups and VM consumers.
The reverse stop order is Proxy/Conductor and other producers, execution pools,
Connector, then network cleanup. Component restart does not tear down the switch
or stop existing Guests. Remaining allocated slots may be forced only after
producers and consumers have ended. Failed cleanup keeps the applied record.

For production recovery, a short-lived helper pins necessary namespaces and
private bpffs before PID 1. The main container receives `rslave` pins; ordinary
internal cleanup dismantles objects but leaves the host-owned pins intact. After
the old container and helpers have ended, the outer hook invokes the same cleanup
primitive with the recorded image/tool/generation/boot identity and releases its
own bindings. Changed current configuration cannot redirect cleanup. Older
schema-1 records continue through their pinned old tools and images.

The installed outer unit retains Quadlet's `KillMode=mixed`. Normal stop uses
Podman's image stop signal and the internal ordered shutdown, keeping storage
helpers alive until the container exits. If conmon dies, systemd kills the
remaining instance cgroup before recovery; this abnormal path terminates Guests
immediately. Sending a soft signal to the entire group could kill a FUSE storage
helper before PID 1 can finish shutting down. Recovery still checks that all
consumers have ended before releasing pins.

SIGKILL, `docker rm -f` and power loss cannot run internal teardown. Default
Docker without borrowed devices uses private kernel objects; the business data
volume does not contain live namespace handles. Borrowed transit requires the
explicit retained-handle prepare/recover path to guarantee return after hard
exit. Never replace a missing old handle with a new same-named namespace. Across
boot boundaries the host archives old journals separately; it does not treat old
JSON as a live reference. Automatic restart is disabled while recovery is pending.
Use status and the outer journal to identify failed production recovery, correct
the stated input, then retry:

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

- `basic.node-infra-deploy.sh`: default host/no-transit and explicit namespace/transit
  deployment, real guest Exec/Proxy/MMDS, cgroup limits, memory and E-only restore,
  host hard exits/helper interruption, resource preservation, conflicts and the
  other two network/transit combinations.
- `basic.node-infra-config.sh`: layered native configuration, SDK extensions and
  private service input, worker rebuild, two actual execution pools and partial
  startup failure cleanup.
- `basic.node-infra-lifecycle.sh`: two live instances, PID 1 and conmon hard
  exit/recovery, changed configuration, cleanup conflict/retry with missing BPF
  metadata, restart and stop isolation.

Preparation validates the matching deployment package and both archives when
using `node-infra-v2`; execution imports OCI into Podman and Docker archive into
Docker, requiring identical configuration and ordered rootfs diffIDs.
`basic.node-infra-docker.sh` exercises direct host/bridge startup, component
restart, real Files/Build/OTLP, normal/hard stops, snapshots, external MGMT/egress
TCP/UDP SNAT and failure rollback, cgroupfs, retained transit and three-instance
isolation. Preparation also creates a COPY-only customer fixture from the
selected image and prebuilt SDK helpers; execution consumes its sealed archive
to test copied configuration together with runtime fragments. The Podman
path uses the delivered tool/template. The guest
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
