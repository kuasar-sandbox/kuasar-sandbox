[English](README.md) | [简体中文](README_zh.md)

# node-infra configuration examples

These files extend one delivered kuasar-sandbox node-infra instance. The
[deployment guide](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/main/docs/node-infra.md) owns host prerequisites,
installation, operation and recovery. In an extracted deployment package, use
the accompanying deployment guide. Component repositories own complete schemas.

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
