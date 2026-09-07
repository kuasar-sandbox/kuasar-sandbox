[English](QUICKSTART.md) | [简体中文](QUICKSTART_zh.md)

# Platform release validation guide

> This is the Aggregate Release Validation Guide for full E2E and release acceptance, not a first-installation guide.
> Users getting started should read the [Quick Start](../docs/quickstart.md) first.

This file is delivered as `test/QUICKSTART.md` in `platform-release-vX.Y.Z.tar.gz`. The platform package contains system and component documentation, component E2E suites, platform combination tests, performance scripts and the Demo. The six component packages contain runtime artifacts. Extract the seven archives from one aggregate Release into the same directory to run full validation.

<a id="1-解包布局"></a>
## 1. Extracted layout

```text
<release-dir>/
├── bin/                         Runtime artifacts from the six component packages
├── deploy/                      Component deployment files
├── docs/                        Aggregated platform and five-component documentation
└── test/
    ├── QUICKSTART.md
    ├── e2e/
    │   ├── run_all.sh           Full release gate
    │   ├── accelerator/
    │   ├── connector/
    │   ├── guest-runtime/
    │   ├── sandboxer/
    │   ├── orchestrator/
    │   └── platform/            Genuinely cross-component combination tests
    ├── perf/
    └── demo/
```

Each owner directory has one stable entry point, `run_all.sh`. Component-feature cases stay in that component's directory even when they depend on another repository's binaries, KVM or the service environment provided by the platform. The platform directory contains only combinations that cannot be assigned to a single component.

<a id="2-解压与校验"></a>
## 2. Verification and extraction

Download all explicit assets from the same aggregate Release, then verify them first:

```bash
sha256sum --quiet -c SHA256SUMS
mkdir kuasar-sandbox-release
for archive in *.tar.gz; do
    tar -xzf "$archive" -C kuasar-sandbox-release
done
cd kuasar-sandbox-release
```

The release process checks archive path safety and cross-package overwrites. Do not mix `SHA256SUMS`, platform packages or component packages from different aggregate Releases.

<a id="3-前置条件"></a>
## 3. Prerequisites

The full gate requires:

- Linux x86_64, systemd, cgroup v2 and readable/writable `/dev/kvm`;
- root or noninteractive `sudo`;
- Docker, iproute2, curl, Python 3, openssl and mkfs.ext4;
- an executable local OCI registry, `zot`;
- an executable S3-compatible test gateway, `versitygw`;
- access to the base images used by the cases, or those images prepared in advance.

By default, `bin/` is located from the extraction root; override it with `BIN=/path/to/bin`. The OBS cases are the only credential-dependent optional suite and run only when `OBS_E2E=1` is set.

<a id="4-运行完整门禁"></a>
## 4. Run the full gate

```bash
ZOT_BIN=/path/to/zot \
VGW_BIN=/path/to/versitygw \
bash test/e2e/run_all.sh
```

The top-level entry point runs `run_all.sh` for accelerator, connector, guest-runtime, sandboxer, orchestrator and platform in that order. It stops on any owner failure. Successful completion ends with:

```text
==> full release e2e: OK
```

<a id="5-运行组件或单项用例"></a>
## 5. Run an owner or individual case

Run one owner:

```bash
BIN=$PWD/bin bash test/e2e/accelerator/run_all.sh
BIN=$PWD/bin bash test/e2e/sandboxer/run_all.sh
BIN=$PWD/bin ZOT_BIN=/path/to/zot VGW_BIN=/path/to/versitygw \
    bash test/e2e/orchestrator/run_all.sh
```

For a directly invoked case, set the environment described at the top of that script:

```bash
BIN=$PWD/bin bash test/e2e/sandboxer/e2e_sandbox_cold.sh
BIN=$PWD/bin ZOT_BIN=/path/to/zot \
    bash test/e2e/orchestrator/e2e_run_builder.sh
```

The main ownership boundaries are:

- accelerator: Manifest, Cache, Store and OBS;
- connector: eBPF/TC network topology and TAP;
- guest-runtime: OCI flattening and runtime bundle;
- sandboxer: cold start, disks, snapshot, restore, stdio and TAP FD;
- orchestrator: node/proxy, builder, exec, MMDS, cluster and resource density;
- platform: cross-component sandboxer/accelerator integration, currently including the legacy-named `e2e_warmpool_dedup.sh` case. The test name is not a general cross-VM snapshot-deduplication guarantee.

<a id="6-perf-与-demo"></a>
## 6. Performance and Demo

Performance entry points are under `test/perf/`; the E2B SDK Demo is under `test/demo/`. They share the same `bin/` layout with E2E but are not part of the component PR `run_all.sh` correctness gate. Common entry points:

```bash
BIN=$PWD/bin bash test/perf/sandbox-perf.sh
bash test/demo/demo_prep.sh
sudo bash test/demo/demo_e2b.sh
```

<a id="7-排错"></a>
## 7. Troubleshooting

- `missing executable .../run_all.sh`: the platform package/component selection is incomplete, or packages from different versions were mixed.
- `missing ... in BIN`: not all six component archives were extracted.
- `/dev/kvm` unavailable: check device permissions and runner virtualization configuration.
- `sudo -n` fails: configure the test runner with the required noninteractive permissions.
- Image pull fails: pre-pull the images specified by the script or configure a reachable image proxy.
- Preserve a failure environment: set `E2E_KEEP=1` when supported by the specific script.
