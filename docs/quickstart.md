[English](quickstart.md) | [简体中文](quickstart_zh.md)

# Quick Start

Run a real Kuasar MicroVM with the unmodified E2B Python SDK using a matching
aggregate release and workbench. This is the recommended first-use path; native
production deployment remains independent of workbench and is covered by
[Deployment](deployment.md).

The short Demo builds a snapshot template, creates a sandbox, executes commands,
reads/writes files, checks network access, pauses/resumes and destroys the sandbox.
It is a first-use demonstration, **not full E2E or release qualification**.

## 1. Acquire one supported release

Use native Linux x86_64 or aarch64, a local rootful Docker Engine, Python 3.9+ and
Docker access. This is a trusted administrator environment. Docker Desktop,
remote Docker endpoints and architecture emulation are not supported. Actual
MicroVM execution needs readable/writable KVM and the required kernel features;
workbench does not emulate missing hardware or change host kernel policy.

The image supplies systemd, private Docker/containerd, product user-space
libraries, Python 3.12 and ordinary tools. The host does **not** need the Demo SDK,
product glibc version, Go/Rust, or systemd as PID 1 just to launch workbench.
Keep CPU, memory and disk headroom for the host and other workloads. The example
uses a 4-CPU/12-GiB workbench budget, not a universal minimum or a full-suite
capacity guarantee; the Demo builder alone requests 2 vCPUs and 6 GiB capacity.

Follow [Acquire a matching aggregate release](download.md) once. It selects a
concrete published version, validates its delivery contract and native assets,
extracts products/materials and separately imports workbench. It returns
`RELEASE` (absolute extracted directory), `IMAGE` and `ARCH`. Keep these in the
same **host Bash shell** for the commands below. Use only that version's launcher,
Demo, helpers, wheelhouse and products; old releases without the prepared Demo
adapter must use their own bundled guide, not scripts from `main`.

## 2. Start a private system environment

These commands run on the **host**. State must be outside the release tree and
owned by the invoking UID. Do not alternate users or prepend `sudo` to only some
launcher commands. Choose a new instance name if a previous instance was cleaned.

```bash
STATE="$PWD/kuasar-workbench-state"
NAME="demo-$(date +%s)"
WB="$RELEASE/workbench/workbench"
python3 "$WB" check --image "$IMAGE" --mode system
python3 "$WB" --root "$STATE" --name "$NAME" start \
  --image "$IMAGE" --mode system --inputs "$RELEASE" \
  --cpus 4 --memory-gib 12 --network bridge
```

`check` checks host/image compatibility; `start` checks private systemd/daemons and
reports hardware capabilities. Neither proves the Demo passed. The instance has
private networking and services: host `127.0.0.1` is not workbench `127.0.0.1`.
Do not expose the host Docker socket or use host networking to work around this.

## 3. Prepare local inputs

The outer command runs on the **host**; the command after `exec --` runs inside
**workbench**. `/inputs/release` is read-only. The public prepare command creates
a new immutable `/work/prepared`, using only the selected image archives, helper
binaries and hash-locked SDK wheels. Do not create a virtual environment under
`/inputs/release` or install a substitute SDK from the network.

```bash
python3 "$WB" --root "$STATE" --name "$NAME" exec -- \
  python3 -B /inputs/release/test/e2e/e2e prepare \
  --release-dir /inputs/release --workdir /work/prepared --arch "$ARCH" \
  --include basic.demo.sh --deps-dir /opt/workbench/deps --offline
```

`--offline` prohibits dependency downloads after artifacts are acquired. It does
not disable local Registry/Store/Proxy traffic, and does not make the subsequent
Demo an offline workload. **Keep `--network bridge`: even the short Demo checks
real Internet egress.** Do not use `DEMO_NETDIAG` to turn a failed assertion into
success. A missing input fails preparation; it is not silently replaced.

## 4. Run the first sandbox

```bash
python3 "$WB" --root "$STATE" --name "$NAME" exec -- \
  python3 -B /work/prepared/test/demo/prepared.py run \
  --workdir /work/prepared --data-dir /work/kuasar-demo-first --quick
```

The adapter verifies the prepared inputs, loads the exact native image and uses
the existing Demo scripts with the local SDK/helper environment. Sandbox
commands execute inside the **Guest**. Mutable state and retained logs live at
`/work/kuasar-demo-first`, separate from the prepared input tree.

Success requires the actual template, MicroVM, command/file, network,
pause/resume and destruction assertions and a zero exit status. The Demo prints
its stages; it does not leave a production node serving after completion. Run the
same command again to reuse persistent preparation with a new run identity.

For full COPY, template fan-out, migration or interactive observation, see
[Demo](../test/demo/DEMO.md). For the complete ordinary case selection and result
JSON, see [Release validation](../test/QUICKSTART.md).

## 5. Inspect and clean up

The adapter retains Demo logs under `/work/kuasar-demo-first/results/` when safe
to retain, and preparation logs under its `logs/`. Private diagnostic files may
contain sensitive state; inspect them locally before sharing. They map to
`$STATE/$NAME/work/kuasar-demo-first` on the host. Startup diagnostics are under
`$STATE/$NAME/output`. Use the same workbench instance to inspect root-owned data.

Stop the owned Demo preparation, stop workbench, then remove its container,
network and daemon data:

```bash
python3 "$WB" --root "$STATE" --name "$NAME" exec -- \
  python3 -B /work/prepared/test/demo/prepared.py stop \
  --workdir /work/prepared --data-dir /work/kuasar-demo-first
python3 "$WB" --root "$STATE" --name "$NAME" stop
python3 "$WB" --root "$STATE" --name "$NAME" cleanup
```

`cleanup` keeps work/build/home/journal/output directories. `cleanup
--delete-output` explicitly deletes them; use it only after retaining needed
results. Demo `reset` also deletes that Demo's retained logs and data, so it is
not the default failure-recovery step. A stopped instance can restart with its
original image/mode/mounts/budgets; a cleaned instance needs a new name. Full
lifecycle semantics are in [Workbench](../workbench/README.md).

## Troubleshooting

A historical-contract or missing-adapter error means the selected version does
not supply this workflow. Explicitly choose a suitable published version or its
historical native guide. Do not silently switch channels or mix script versions.

For missing KVM/kernel capabilities, choose a native host that supplies the
required features. For a failed prepare, fix its stated missing/corrupt input and
use a fresh prepared directory. For listener/unit/network conflicts inside a
workbench, inspect that instance's prior Demo; do not kill unrelated host services.
A nonzero Demo or cleanup exit is a failure, not a successful skipped check.
