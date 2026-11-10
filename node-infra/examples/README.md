[English](README.md) | [简体中文](README_zh.md)

# node-infra configuration examples

These files extend one delivered kuasar-sandbox node-infra instance. The
[deployment guide](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/main/docs/node-infra.md) owns host prerequisites,
installation, operation and recovery. In an extracted deployment package, use
the accompanying deployment guide. Component repositories own complete schemas.

## Default host network and optional transit

Start with the delivered `Network=host` template and omit transit fields. Supply
the native component configuration, external storage and persistent data paths
as described in the deployment guide; no primary netns or interface renaming is
needed. Proxy and management traffic work without an ordinary guest egress.

For explicit isolation use `Network=ns:/run/netns/node-a`, with loopback UP and
an ingress interface of any suitable name. Independently, `transit/90-transit.yaml`
is an opt-in native overlay: configure its real dedicated DOWN device, address
and gateway, then mount or COPY it to `/etc/connector-vswitch/conf.d/90-transit.yaml`.
Do not select an interface carrying host traffic. Both examples keep the same
deployment tool, recovery mounts and service configuration paths.

## Multiple resource pools

Mount `multi-pool/50-platform.yaml` at
`/etc/node-conductor/conf.d/50-platform.yaml` and the two `.conf` files at
`/etc/node-infra/placement/p0.conf` and `p1.conf`. The example leaves management
headroom beneath an outer 8 CPU / 32 GiB limit. Set budgets and optional
`AllowedCPUs`/`AllowedMemoryNodes` for the actual host before use; the fragments
do not assume a host CPU or NUMA numbering scheme.

Runner and Builder lists are both replaced. Their resource-pool references and
distinct templates remain native Conductor fields. When layering over an older
configuration containing `units.runner`, `runner_pool_size`, `builder`,
`builder_pool_size` or the legacy physical-memory formula, remove those inherited
keys with an overlay `null` before switching to explicit pools. The service
launcher passes the base plus sorted explicit files to the native loader.

## Customer xproxy and xconductor image

Use `extensions/` as a build context:

1. Build `out/xproxy` and `out/xconductor` against the orchestrator SDK recorded
   in the selected base image's `/usr/share/node-infra/image.json`.
2. Replace `private/r42/*.yaml` with your applications' private configuration.
   The example environment-variable names are application-defined: the
   applications must read them. They are not node-infra configuration keys.
3. Build the Containerfile with a pinned `BASE_IMAGE`, then use the derived
   immutable image identity in the instance's Quadlet.

```sh
podman build --build-arg BASE_IMAGE=ghcr.io/kuasar-sandbox/node-infra@sha256:BASE_DIGEST \
  --tag localhost/node-infra-custom:r42 --file extensions/Containerfile extensions
```

In the Quadlet template, remove configuration directory mounts that would hide
the copied files, or replace them with individual site-file mounts. Keep the
data and recovery mounts. Use the full `sha256:` configuration ID for the
derived image; its mutable build tag is not an installation identity.

The service still starts official `node-ctl conductor serve` or `node-ctl proxy
serve`; the native bootstrap executes the selected application. Keep official
programs and sibling helpers in `/opt/node-infra/bin/`. The sample installs
root-owned executable files and places environment settings on the component
services through `EnvironmentFile`.

Mounting these same individual files at runtime provides the same configuration
interface as COPY. A versioned private path keeps Proxy worker restarts tied to
the same private configuration. Restart the affected service or replace the
instance through the normal maintenance flow to activate a new version.

`node-ctl config` validates declarative input; it does not execute custom
Configure/BindRuntime hooks. Verify real startup, the intended custom request
path and worker restart with the selected SDK/image combination. The platform
`basic.node-infra-config.sh` case demonstrates this against test-only SDK
applications, using the delivered deployment entry point.

## External egress and site network hooks

The default MGMT plane is private `mgmt`; API/data ingress stays in the primary
network. Mount or COPY `external-egress/90-egress.yaml` as a Connector overlay
and bind only the provider's nsfs handle to `/run/node-infra/netns-inputs/egress:ro`.
The provider owns the exit, routes and SNAT. Keep the metadata extraction first.

For custom setup, install `network-extension/node-site-network` as executable
`/usr/local/libexec/node-site-network`, its unit under `/etc/systemd/system/`,
and its Connector drop-in under `/etc/systemd/system/connector-vswitch.service.d/`.
Allocate a unique nft table name to the instance in the script. The oneshot is
required by the drop-in, so no separate enable command is needed. The script is
idempotent and only deletes that allocated table. Preserve the base ready hook;
appended `ExecStartPost` checks can fail startup. nftables is included in the
image. Install other tools while building a derived image.

The deployment guide also provides direct Docker host/bridge commands with the
same native files, private cgroups, normal internal cleanup and distinct ports
for three instances. Optional borrowed transit uses retained recovery handles;
the default Docker path needs no host install or prepare helper.
