[English](QUICKSTART.md) | [简体中文](QUICKSTART_zh.md)

# Platform release validation

This guide validates prebuilt product cases. For the first sandbox, use
[Quick Start](../docs/quickstart.md); a short Demo is not full release acceptance.
The public runner is the only product E2E execution path. Workbench supplies an
environment, not another runner, product bundle or case selector.

<a id="1-extracted-layout"></a>
## 1. Release inputs and layout

Use [Acquire a matching release](../docs/download.md) to obtain one explicit
aggregate's native products, platform materials and matching workbench image.
The recipe validates every selected required file even when other architecture
assets are intentionally not downloaded. Do not run a blanket checksum command
that requires all unselected files, or ignore missing required files. Import the
workbench Docker archive separately; never unpack it into this tree.

```text
<release-dir>/
├── bin/                         Native products from component archives
├── guide/                       Selected bilingual user guides
├── workbench/                   Host launcher and its guide
└── test/
    ├── QUICKSTART.md
    ├── e2e/
    │   ├── e2e                  Public list / prepare / run
    │   ├── cases/               <suite>.<case>.sh
    │   ├── lib/                 Owner-namespaced runtime helpers
    │   └── helpers/<arch>/      Prebuilt programs and identity records
    └── demo/                    Demo scripts, prepared adapter and locked wheels
```

The platform package contains `guide/`, public tests/runtime inputs and the thin
workbench launcher. Component archives may also add their own `deploy/`, `share/`
and legal material. Source-only tests, assembly tools and `test/perf/` harnesses
are not shipped in the new platform package. Historical releases may have a
`docs/` layout; use their matching guide rather than assuming the new layout.

Full filenames are case IDs; their first segment is one of `basic`, `storage`,
`image`, `network`, `sandbox`, `snapshot`, `orchestrator`, `builder`, `telemetry`.
Ownership determines source maintenance, not a separate runner or selector.

<a id="2-verification-and-extraction"></a>
<a id="3-prerequisites"></a>
## 2. Environment and actual coverage

The recommended environment is native workbench system mode. Host prerequisites
and mode boundaries are in [Workbench](../workbench/README.md). Workbench supplies
systemd, Docker, Python 3.12, EROFS readers and ordinary tools; each selected case
still needs real KVM/kernel/network/device capabilities. Generic startup is not
a product pass. Keep resource headroom; example CPU/memory budgets are starting
points, not universal full-suite capacity guarantees.

Native x86_64 and aarch64 use the same current ordinary selection:
`--all --exclude storage.obs.sh`. ARM KVM cases need the appropriate Guest
PMEM/DAX kernel and native static cgroup probe in the selected release. Missing
required inputs fail prepare; they are not substituted. Hosted ARM without KVM
retains its explicitly narrower non-KVM/artifact-only lane. Historical subset
results cannot be relabelled native-full.

Prepare and execution consume products, exact helper packages and local locked
Demo wheels without compiling them or discovering sibling checkouts. Prepare
reads `/sbin/init` from the actual runtime bundle for telemetry identity, not a
standalone init replacement. Mutable runs/results stay outside sealed inputs.

<a id="4-prepare-and-run"></a>
## 3. Prepare and run in workbench

On the **host**, keep `RELEASE`, `IMAGE` and `ARCH` from acquisition. Everything
after `exec --` runs **inside workbench**:

```bash
STATE="$PWD/kuasar-validation-state"
NAME="verify-$(date +%s)"
WB="$RELEASE/workbench/workbench"
python3 "$WB" --root "$STATE" --name "$NAME" start \
  --image "$IMAGE" --mode system --inputs "$RELEASE" \
  --cpus 8 --memory-gib 16 --network bridge
python3 "$WB" --root "$STATE" --name "$NAME" exec -- \
  python3 -B /inputs/release/test/e2e/e2e prepare \
  --release-dir /inputs/release --workdir /work/prepared --arch "$ARCH" \
  --all --exclude storage.obs.sh --deps-dir /opt/workbench/deps --offline
python3 "$WB" --root "$STATE" --name "$NAME" exec -- \
  python3 -B /work/prepared/test/e2e/e2e run --workdir /work/prepared --arch "$ARCH" \
  --all --exclude storage.obs.sh --run-root /work/run-1 \
  --out-root /output/cases --result /output/result.json
```

Offline prepare means no dependency downloads. Full execution includes Demo
Internet egress and therefore needs bridge networking. Local Guest/Registry/
Store/Proxy communication remains permitted. The credentialed OBS case requires
explicit selection plus its documented credentials/network; it is not silently
skipped or included in ordinary public validation.

Use a fresh prepared directory; preparation seals only complete inputs. Rerunning
an immutable prepared set is allowed with a fresh `--run-root` and result/output
paths. `PASS <filename>` and `result.json` report only cases actually executed.
Any nonzero exit, missing prerequisite or changed input fails. The runner keeps
case filenames, durations and exit codes; inspect console output and case files,
not just the final aggregate status.

### Native alternative

The same runner can execute directly on a native systemd/cgroup-v2 host with
root/noninteractive sudo, required devices, Docker, Python 3.12, EROFS readers
(`fsck.erofs --extract`, `dump.erofs --cat`) and the selected cases' ordinary
tools/libraries. Set `RELEASE` to a verified extracted tree and `ARCH` to the
native architecture. There is no workbench dependency for this path:

```bash
release_dir="$RELEASE"
prepared="$PWD/kuasar-prepared"
python3 -B "$release_dir/test/e2e/e2e" prepare --release-dir "$release_dir" \
  --workdir "$prepared" --arch "$ARCH" --all --exclude storage.obs.sh
sudo -n python3 -B "$prepared/test/e2e/e2e" run --workdir "$prepared" \
  --arch "$ARCH" --all --exclude storage.obs.sh --result "$PWD/kuasar-e2e-result.json"
```

The following local-input contract applies inside workbench and on native hosts.
For the native examples, `release_dir` names the extracted input tree; workbench
already defaults to its validated `/opt/workbench/deps` and `E2E_OFFLINE=1`.
### Local and offline preparation

After acquiring and verifying the release and dependency inputs, select a local image directory:

```bash
python3 "$release_dir/test/e2e/e2e" prepare --release-dir "$release_dir" \
    --workdir /var/tmp/offline-prepared --arch "$ARCH" --all --exclude storage.obs.sh \
    --deps-dir /inputs/deps --offline
```

`E2E_DEPS_DIR` is the directory alias; `--deps-dir` takes precedence. `E2E_OFFLINE` accepts only `0` or `1`. `--offline` always prohibits dependency downloads, including when `E2E_OFFLINE=0`. With neither flag nor environment configuration, preparation retains its online behavior. `E2E_OFFLINE=0` without `--offline` permits remote fallback for missing inputs. An explicitly empty, missing or symbolic-link directory fails. When using `sudo`, pass these options explicitly after the privilege transition; do not preserve the whole user environment.

The directory contains a flat `images.json` list and Docker image archives. Each record has `reference`, `platform` (`linux/amd64` or `linux/arm64`), `image_id` (config SHA-256), relative `archive`, and `sha256` (archive bytes). Registry evidence consists of `manifest` (the exact raw JSON text), `manifest_digest`, and `registry_digest`; an index response also includes exact raw `index` text and `index_digest`. These are separate identities. The manifest must hash to its recorded digest and bind the archive's actual config; every archived layer must match that config's uncompressed layer digest. An index must bind exactly one matching platform manifest. An `@sha256:...` request must match the verified registry response digest. A self-declared digest field alone is rejected. A moving tag's recorded resolution is consumed without a remote freshness check; obtain the directory from verified release inputs, since offline content checks cannot authenticate an arbitrary author's tag mapping.

Preparation matches the exact requested reference and platform. Valid selected archives are copied without loading/saving them again; only locally derived orchestrator fixtures need to load the Python base into Docker during preparation. Missing selected inputs fail offline with their reference, platform and search location. Online mode may download missing inputs, but corrupt, unsafe, ambiguous or wrong-identity matches always fail without remote repair. Archives and description paths cannot escape the directory or use symbolic links; unsafe archive paths, duplicate members, hard links and devices are rejected. The only permitted symbolic archive members are Skopeo’s legacy `<hex>/layer.tar` aliases to `../<diff-id>.tar`: each must target a manifest-selected regular layer whose bytes match the config digest. Verification never follows these aliases; selected configs and layers must remain regular files. Unselected archives are not prerequisites. Completed preparation records image evidence and hashes/modes in `provenance.json`; failed preparation does not create the requested workspace.

Offline controls dependency acquisition. It neither changes case selection nor prohibits local Guest/Registry/Store/Proxy traffic. Helpers still come from their exact package and the Demo SDK still installs exclusively from its local hash-locked wheelhouse. Credentialed OBS is never silently skipped. CI's `prepare-artifacts.py` passes the same options to the public runner and mounts a configured dependency directory read-only for clean preparation, separately from writable output.


<a id="5-select-suites-or-individual-cases"></a>
## 4. Select suites or individual cases

```bash
python3 "$WB" --root "$STATE" --name "$NAME" exec -- \
  python3 -B /inputs/release/test/e2e/e2e list --suite storage
python3 "$WB" --root "$STATE" --name "$NAME" exec -- \
  python3 -B /inputs/release/test/e2e/e2e prepare --release-dir /inputs/release \
  --workdir /work/storage-prepared --arch "$ARCH" \
  --suite storage --exclude storage.obs.sh --offline
python3 "$WB" --root "$STATE" --name "$NAME" exec -- \
  python3 -B /work/storage-prepared/test/e2e/e2e run --workdir /work/storage-prepared \
  --suite storage --exclude storage.obs.sh --result /output/storage-result.json
```

Repeated `--suite` and `--include` selections are unioned; `--exclude` removes
matching filenames. Unknown/empty selections fail, as does running a case that
was not prepared. A suite may span owners. Do not introduce another owner/tag/
capability/fixture selector. See [Test organization](README.md).

<a id="6-performance-and-demo"></a>
## 5. Demo, source checks and performance

`basic.demo.sh` uses prepared products, image, SDK and helpers to exercise the
full real Demo plus ownership/repeated-preparation assertions. Its short and
network-diagnostic controls are cleared, so a caller cannot accidentally reduce
acceptance. [Demo](demo/DEMO.md) separately offers quick/full/interactive user
runs using the same underlying scripts; those runs do not claim the extra full
case or complete suite acceptance.

Performance harnesses stay in the source tree under `test/perf/`; use the
[source performance guide](../docs/perf.md), not paths assumed to exist in the
platform archive. Unit/race/vet, UFFD and working-set source gates remain
independent. A smoke run is not statistical performance qualification.

<a id="7-troubleshooting-and-acceptance"></a>
## 6. Results, cleanup and acceptance

Workbench results map to `$STATE/$NAME/output`; mutable case state maps to
`$STATE/$NAME/work`. Inspect root-owned private diagnostics inside the same
instance before sharing them. Once inspection is complete:

```bash
python3 "$WB" --root "$STATE" --name "$NAME" stop
python3 "$WB" --root "$STATE" --name "$NAME" cleanup
```

Default cleanup retains work/output and journals. Explicit `--delete-output`
removes them; do not erase evidence as a default retry step. Missing/corrupt inputs
require a fresh preparation from verified sources, not remote replacement during
execution. Conflicting resources must be handled by their actual owner.

Developer candidate results must identify their script/product/helper revisions
and environment; they are not final published-byte qualification. Formal release
acceptance additionally requires exact released identities, the declared native
coverage and the independent compiler/source-free runtime gate. Workbench's
included toolchains do not prove that gate. Keep failures and exclusions visible.
