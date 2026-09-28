[English](QUICKSTART.md) | [简体中文](QUICKSTART_zh.md)

# Platform release validation guide

This guide covers aggregate product E2E and release acceptance. For first installation, use the [Quick Start](../docs/quickstart.md).

The platform package contains documentation, the common runner, flat cases, prebuilt test helpers, performance scripts and the Demo. Six component packages contain runtime artifacts. Use the platform archive and six component archives for one architecture from the same aggregate Release.

<a id="1-解包布局"></a>
## 1. Extracted layout

```text
<release-dir>/
├── bin/                         Prebuilt products for one architecture
├── deploy/
├── docs/
└── test/
    ├── QUICKSTART.md
    ├── e2e/
    │   ├── e2e                  Shared list / prepare / run entry
    │   ├── cases/               <suite>.<case>.sh
    │   ├── lib/                 Low-level helpers, namespaced by owner
    │   └── helpers/<arch>/      Prebuilt executables and source/hash manifest
    ├── perf/
    └── demo/
```

The full filename is the case ID. The first segment selects one of nine suites: `basic`, `storage`, `image`, `network`, `sandbox`, `snapshot`, `orchestrator`, `builder`, `telemetry`. There are no owner runners or alternate product execution paths.

<a id="2-解压与校验"></a>
## 2. Verification and extraction

Verify downloaded assets against their Release checksums. Extract only the target architecture's six component archives and the matching platform archive into one fresh directory. Do not extract both architectures over the same `bin/`, or mix different aggregate Releases.

```bash
sha256sum --quiet -c SHA256SUMS
mkdir kuasar-sandbox-release
# Replace these names with the explicit selected Release assets.
tar -xzf platform-release-vX.Y.Z.tar.gz -C kuasar-sandbox-release
# Extract the six selected component archives into this directory too.
```

Packaging validates archive paths, cross-package collisions, helper architecture, hashes and independent test source pins. Products and tests may have different source revisions; their identities remain separately recorded.

<a id="3-前置条件"></a>
## 3. Prerequisites

Full native x86 execution requires Linux, systemd, cgroup v2, usable `/dev/kvm`, root or noninteractive `sudo`, Docker, iproute2, curl, Python 3.11+ (Python 3.12 for the packaged Demo SDK), openssl, EROFS readers, mkfs.ext4 and the ordinary utilities required by the selected scripts. Preparation uses local image inputs when configured and otherwise downloads the selected external images. The hash-locked Python wheels and registry/gateway/probe binaries come from the package.

No Go or Rust compiler or component source checkout is needed during preparation or product execution. The runner rejects missing prepared inputs; it does not substitute host helper binaries or pull images while running cases. Prepared inputs must remain unchanged. Mutable case state and result files live outside the prepared directory.

<a id="4-运行完整门禁"></a>
## 4. Prepare and run

```bash
release_dir="$PWD/kuasar-sandbox-release"
prepared="/var/tmp/kuasar-prepared"
python3 "$release_dir/test/e2e/e2e" prepare --release-dir "$release_dir" \
    --workdir "$prepared" --arch x86_64 --all --exclude storage.obs.sh
sudo python3 "$prepared/test/e2e/e2e" run --workdir "$prepared" \
    --all --exclude storage.obs.sh --result /var/tmp/kuasar-e2e-result.json
```

Use a fresh preparation directory. Preparation acquires immutable image archives and the Demo SDK, records hashes and permissions, and does not compile products or helpers. Execution loads those image archives, invokes each selected Bash case, records its full filename, elapsed time and exit status, and verifies the input tree again. Real tested Build, flatten, snapshot and publication operations remain in the cases.

A nonzero case, missing prerequisite or changed input fails the run. `PASS <filename>` and `result.json` refer only to executed cases. Draft skips and static architecture checks are not product acceptance. `storage.obs.sh` requires explicit selection and its documented external storage credentials; keep it excluded for ordinary public validation.

### Local and offline preparation

After acquiring and verifying the release and dependency inputs, select a local image directory:

```bash
python3 "$release_dir/test/e2e/e2e" prepare --release-dir "$release_dir" \
    --workdir /var/tmp/offline-prepared --arch x86_64 --all --exclude storage.obs.sh \
    --deps-dir /inputs/deps --offline
```

`E2E_DEPS_DIR` is the directory alias; `--deps-dir` takes precedence. `E2E_OFFLINE` accepts only `0` or `1`. `--offline` always prohibits dependency downloads, including when `E2E_OFFLINE=0`. With neither flag nor environment configuration, preparation retains its online behavior. `E2E_OFFLINE=0` without `--offline` permits remote fallback for missing inputs. An explicitly empty, missing or symbolic-link directory fails. When using `sudo`, pass these options explicitly after the privilege transition; do not preserve the whole user environment.

The directory contains a flat `images.json` list and Docker image archives. Each record has `reference`, `platform` (`linux/amd64` or `linux/arm64`), `image_id` (config SHA-256), relative `archive`, and `sha256` (archive bytes). Registry evidence consists of `manifest` (the exact raw JSON text), `manifest_digest`, and `registry_digest`; an index response also includes exact raw `index` text and `index_digest`. These are separate identities. The manifest must hash to its recorded digest and bind the archive's actual config; every archived layer must match that config's uncompressed layer digest. An index must bind exactly one matching platform manifest. An `@sha256:...` request must match the verified registry response digest. A self-declared digest field alone is rejected. A moving tag's recorded resolution is consumed without a remote freshness check; obtain the directory from verified release inputs, since offline content checks cannot authenticate an arbitrary author's tag mapping.

Preparation matches the exact requested reference and platform. Valid selected archives are copied without loading/saving them again; only locally derived orchestrator fixtures need to load the Python base into Docker during preparation. Missing selected inputs fail offline with their reference, platform and search location. Online mode may download missing inputs, but corrupt, unsafe, ambiguous or wrong-identity matches always fail without remote repair. Archives and description paths cannot escape the directory or use symbolic links; unsafe archive paths, duplicate members and links/devices are rejected. Unselected archives are not prerequisites. Completed preparation records image evidence and hashes/modes in `provenance.json`; failed preparation does not create the requested workspace.

Offline controls dependency acquisition. It neither changes case selection nor prohibits local Guest/Registry/Store/Proxy traffic. Helpers still come from their exact package and the Demo SDK still installs exclusively from its local hash-locked wheelhouse. Credentialed OBS is never silently skipped. CI's `prepare-artifacts.py` passes the same options to the public runner and mounts a configured dependency directory read-only for clean preparation, separately from writable output.

<a id="5-运行组件或单项用例"></a>
## 5. Select suites or individual cases

```bash
python3 "$release_dir/test/e2e/e2e" list --suite storage
python3 "$release_dir/test/e2e/e2e" list --suite snapshot --include image.flatten.sh
python3 "$release_dir/test/e2e/e2e" prepare --release-dir "$release_dir" \
    --workdir /var/tmp/storage-prepared --suite storage --exclude storage.obs.sh
sudo python3 /var/tmp/storage-prepared/test/e2e/e2e run \
    --workdir /var/tmp/storage-prepared --suite storage --exclude storage.obs.sh
```

Repeated `--suite` and `--include` selectors form a union, followed by filename exclusions. Unknown suites, unknown filenames and an empty result fail. Running a case that was not prepared also fails. An entire suite can span component owners; source ownership is described in [test organization](README.md).

ARM CI selects accelerator's storage/image cases and guest-runtime's flatten/registry cases, excluding credentialed OBS. Other cases are recorded as excluded from that lane; an ARM build or static lane does not prove those cases work. Source unit/race/vet, helper, UFFD and working-set gates remain separate from the nine product suites.

<a id="6-perf-与-demo"></a>
## 6. Performance and Demo

`basic.demo.sh` runs the documented Demo with prepared products, images and SDK. It preserves real COPY/Build, Quick Start, exec/files, fan-out, migration, persistent preparation and cleanup assertions. Standalone user Demo instructions remain in [the Demo guide](demo/DEMO.md).

Performance harnesses remain under `test/perf/`. Warm-pool characterization is `test/perf/warmpool-dedup.sh`; its name is not a general cross-VM snapshot-deduplication guarantee. UFFD performance and working-set smoke retain independent source gates. A smoke result is not statistical performance acceptance; cite exact revisions, job logs and raw measurements.

<a id="7-排错"></a>
## 7. Troubleshooting and acceptance

- Missing product/helper: use a complete matching release input set; execution has no source or host fallback.
- Changed hash or mode: create a fresh prepared directory from verified release inputs.
- Missing `/dev/kvm`, systemd or privilege: run on a host satisfying that selected case's prerequisites.
- Image or SDK acquisition failure: repair preparation access and start with a fresh directory; running cases will not pull or install replacements.
- Failed case: inspect its output and recorded exit code. Recovery diagnostics retain bounded error vocabulary without raw capabilities.

Acceptance requires exact head/base and test/product/helper provenance, successful required source checks, and real native product results. Clean release acceptance additionally demonstrates execution without component source trees or Go/Rust toolchains, including an entire non-KVM suite, an entire KVM suite and the applicable ARM non-KVM selection. Keep failed and skipped results visible; local fixture tests do not replace public runner acceptance.
