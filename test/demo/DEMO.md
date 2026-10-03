[English](DEMO.md) | [简体中文](DEMO_zh.md)

# E2B Demo in workbench

The Demo drives a real standalone Kuasar node through the unmodified E2B Python
SDK. Workbench provides its system environment; the selected aggregate provides
products, scripts, native helpers and locked SDK wheels. Workbench is not a
production deployment requirement or a hostile-root security boundary.

## 1. Release/workbench workflow (recommended)

Complete [Quick Start](../../docs/quickstart.md) through offline preparation.
Keep its host variables `WB`, `STATE`, `NAME`, `IMAGE`, `RELEASE` and `ARCH`.
The selected release must include this `prepared.py` adapter. Do not copy it from
`main` into an older release. Use that version's own guide instead.

The host invokes `workbench exec`; everything after `exec --` runs inside the
same system instance. `/inputs/release` is read-only; `/work/prepared` is sealed
input, not a place to install packages or create mutable Demo data. Full Demo:

```bash
python3 "$WB" --root "$STATE" --name "$NAME" exec -- \
  python3 -B /work/prepared/test/demo/prepared.py run \
  --workdir /work/prepared --data-dir /work/kuasar-demo-first
```

Add `--quick` for only the first-use lifecycle. Add `--pause` for interactive
observation between stages:

```bash
python3 "$WB" --root "$STATE" --name "$NAME" exec -- \
  python3 -B /work/prepared/test/demo/prepared.py run \
  --workdir /work/prepared --data-dir /work/kuasar-demo-first --pause
```

In a second **host terminal**, restore the same `WB`, `STATE` and `NAME` values
and enter the same instance:

```bash
python3 "$WB" --root "$STATE" --name "$NAME" exec
```

Use the private `cli.env` path printed by the Demo inside that shell. Its TLS CA,
DNS entries, `127.0.0.1:443` control listener, `127.0.0.2:443` data listener and
floating IPs belong to workbench's network view, not the host's. No public host
ports or host Docker socket are required by this demonstration.

The adapter verifies prepared contents/modes, native image and helpers, creates
an isolated SDK launcher outside inputs and calls the existing scripts. It does
not implement another E2E runner or install Python packages during execution.
The scripts are still the common implementation used by `basic.demo.sh`.

Keep bridge networking: offline prepare prohibits dependency acquisition, while
both short and full Demo retain real Internet egress checks. A successful
workbench startup or a diagnostic run is not Demo success. The complete product
case also checks rejection of foreign resources, repeated prep and cleanup;
run it through the [public E2E runner](../QUICKSTART.md) for those assertions.

## 2. What is demonstrated

| Phase | Operation | Required result |
| --- | --- | --- |
| Prepare | Store and Cache over private Unix sockets; local Zot or a selected Registry | Protocol health succeeds and the digest-pinned base image is read back from the destination Registry |
| Configure | `node-ctl config conductor` and `node-ctl config proxy` | Current Conductor and independent Proxy configurations both validate |
| Ready | Conductor `/health`, Proxy TLS response and stats socket | Control and data listeners are independently ready; a data-shaped request is not served by Conductor |
| Tenant | `manifest-key add` and an E2B API key | Registry credentials are attached at key creation without placing passwords on the command line |
| Build | `Template.build(...)` with nonempty start/ready commands | The existing auto target resolves to a memory Sandbox; the exact build's ready status returns kind `snp` and the published template ID used for create |
| Create | `Sandbox.create(template)` | A real Cloud Hypervisor MicroVM becomes usable through the data Proxy |
| Data | `commands.run`, `files.write`, `files.read`, exposed port | Guest execution and both data paths return asserted content |
| State | `pause` then `Sandbox.connect(id)` | Command-written and Files-API data survive snapshot and resume |
| Fan-out | `export-sandbox --to-template`, then `Sandbox.create` | The child passes guest command and Files-API data assertions before it is required to appear in the public list; `starting` rows are intentionally hidden |
| Migration | `Sandbox.connect(id, headers={migration-token})` | Import and resume complete in one SDK call; both data values are asserted again |
| Destroy | `Sandbox.kill` and run-owned cleanup | Sandboxes and all safely attributable ephemeral host resources are gone |

Quick Start mode (`DEMO_QUICKSTART=1`) stops after build, create, execution/data access, pause/resume, and kill. Complete mode requires VersityGW and continues through fan-out and migration.

The SDK forwards `Template.build(headers=...)` to both registration and trigger,
but Kuasar Builder configuration is register-only. The Demo therefore leaves the
target automatic and supplies start/ready commands, then strictly checks the
snapshot result. The pinned SDK returns its registration handle after waiting;
the Demo reads the published, creatable template ID from that exact build's status.


## 3. Repeated runs, logs and cleanup

`demo_prep.sh` owns persistent Store/Cache/Registry and optional COPY Gateway;
`demo_e2b.sh` owns the per-run Conductor/Proxy, units, network, credentials and
sandboxes. The prepared adapter uses `/work/kuasar-demo-first` in these examples,
passes only its task inputs and retains safe Demo logs by setting `DEMO_KEEP=1`.
It does not inherit short/diagnostic modes or a foreign Docker endpoint.

Run the same command again to reuse preparation with a new run identity.
Inspect preparation `logs/`, safe retained `results/` and any reported private
failure directory under the data root. They map to
`$STATE/$NAME/work/kuasar-demo-first` on the host. Inspect root-only files within
workbench or with authorized host access; do not publish private configuration.
Stop preparation while preserving data and logs:

```bash
python3 "$WB" --root "$STATE" --name "$NAME" exec -- \
  python3 -B /work/prepared/test/demo/prepared.py stop \
  --workdir /work/prepared --data-dir /work/kuasar-demo-first
```

Only after saving needed diagnostics, reset the exact owned Demo data:

```bash
python3 "$WB" --root "$STATE" --name "$NAME" exec -- \
  python3 -B /work/prepared/test/demo/prepared.py reset \
  --workdir /work/prepared --data-dir /work/kuasar-demo-first
```

`reset` deletes that Demo's logs as well as its state. It is not a default response
to failure. Workbench `stop` and `cleanup` are separate outer lifecycle operations;
ordinary `cleanup` retains `/work` and `/output`, while `--delete-output` deletes
them explicitly. See [Workbench](../../workbench/README.md). A cleaned instance
needs a new name; a stopped instance can restart only with its original settings.

## 4. Native source alternative

Use one recorded six-repository source set and the matching built products. This
path runs directly in the native system environment, not in ordinary-UID
workbench build mode. It requires systemd PID 1, cgroup v2, root/noninteractive
sudo, KVM, the product's native libraries, Python 3.12 and the ordinary Demo tools
(openssl, ip, curl, sqlite3, iptables, flock, setsid, timeout, mkfs.ext4 and Docker).
Enable forwarding according to the native host policy before execution; the Demo
does not change it. The two loopback TLS listeners and Demo resources must be
free. Native production topology is documented in [Deployment](../../docs/deployment.md).

From the parent of the six sibling repositories:

```bash
ARCH="$(uname -m)"
make -C kuasar-sandbox build e2e-tools
python3 -m venv kuasar-sandbox/.demo-venv
kuasar-sandbox/.demo-venv/bin/python3 -m pip install \
  --requirement kuasar-sandbox/test/demo/requirements.txt
DEMO_DATA_DIR=/var/lib/kuasar-demo-source
BIN="$PWD/kuasar-sandbox/bin/$ARCH"
PYTHON_BIN="$PWD/kuasar-sandbox/.demo-venv/bin/python3"
ZOT_BIN="$PWD/kuasar-sandbox/build/e2e-tools/$ARCH/zot"
VGW_BIN="$PWD/kuasar-sandbox/build/e2e-tools/$ARCH/versitygw"
sudo -n env DEMO_DATA_DIR="$DEMO_DATA_DIR" BIN="$BIN" \
  ZOT_BIN="$ZOT_BIN" VGW_BIN="$VGW_BIN" \
  bash "$PWD/kuasar-sandbox/test/demo/demo_prep.sh"
sudo -n env DEMO_DATA_DIR="$DEMO_DATA_DIR" BIN="$BIN" \
  PYTHON_BIN="$PYTHON_BIN" \
  bash "$PWD/kuasar-sandbox/test/demo/demo_e2b.sh"
```

`make demo PYTHON_BIN=/absolute/path/to/python` is a **source-only** convenience
entry: it checks the interpreter before building products/tools. It is not a
release installer. Workbench ordinary-UID `--mode build` supplies compilers, not
the privileges/devices needed by `Template.build()` or a MicroVM. Its source
build instructions are in [Workbench](../../workbench/README.md).

Standalone native scripts retain `DEMO_QUICKSTART`, `DEMO_PAUSE`, `DEMO_KEEP` and
`DEMO_NETDIAG` controls; pass them explicitly across `sudo -n env`. The public
full product case never uses a short or network-diagnostic mode. Complete Demo
requires VersityGW for COPY. All paths crossing sudo must be absolute.

The default logical base is `python:3.12-slim`; native platform selection is
`linux/amd64` on x86_64 and `linux/arm64` on aarch64. Standalone preparation can
pull a missing name/tag/digest; artifact execution consumes the exact prepared
local image ID and never selects or pulls a replacement. Wrong-platform images
are rejected. Preparation publishes the same config identity and reads back the
destination manifest digest. Source image ID and registry digest are different
identities. Existing Registry credentials and `REGISTRY_INSECURE=1` remain
explicit native-script choices; the release adapter uses its prepared local Zot.

The template creates the default `user` (UID/GID 1000:1000) with Builder USER
steps before COPY/startup. A replacement source image needs `useradd` if that
account is absent. New sparse overlay/builder disks use `mkfs.ext4 -O ^has_journal`;
this omits the filesystem journal, not application/journald logs, and does not
change Guest sync or snapshot/restore semantics.

## 5. Ownership and credentials

The native script contract below also applies **inside workbench**. Here “host”
means the Demo's execution environment. It does not authorize changes to the
outer Docker host. The adapter uses the explicit `/work` data directory above;
the standalone scripts retain their native default.

`DEMO_DATA_DIR` defaults to `/var/lib/kuasar-demo`. It must be a canonical absolute path using safe path characters. The scripts create a root-owned mode-0700 directory and a strict ownership marker. A nonempty unmarked directory, symbolic-link path, unexpected PID record, changed live configuration, or foreign host resource causes a fail-closed refusal.
An unmarked nonempty directory or invalid ownership marker is rejected before changing the directory's contents or permissions.
The default switch name is `k` plus the first eight run-ID characters. Custom
`SWITCH` names must be 1–9 safe characters, leaving room for Connector's
`-dummy` and port suffixes within Linux's 15-byte interface-name limit.
Name conflicts are refused, never adopted.
New child processes have up to 30 seconds to finish their setsid/exec transition;
their observed executable and start time are tracked before service readiness is
checked. This transition handling never applies to a pre-existing PID record.

Each run reserves the short socket alias `/run/kd-<run-id>` pointing into its private work directory, keeping maximum Sandbox and Build socket paths within Linux's limit. An existing alias is a conflict. Cleanup removes only an alias that still points to that run's directory.

`prep.env`, Docker authentication, service configuration, generated keys, TLS private keys, and the optional `cli.env` are kept in the run-owned tree with private permissions. The preparation handoff uses shell assignments rather than exported secrets, and long-lived Conductor, Proxy, Store, and Cache processes do not inherit Registry or object-storage passwords unnecessarily. Credentials copied into a newly created node business record remain attached to that record; changing a later key-distribution entry does not rebind existing records.

On normal exit and ordinary failures, `demo_e2b.sh` stops only the exact unit instances and process groups it started, deletes only its tagged iptables and `/etc/hosts` entries, and removes a vSwitch or namespace only after proving ownership. It never wildcard-stops all sandbox runners/builders. If ownership becomes ambiguous or cleanup is incomplete, it preserves the private work directory and reports failure instead of force-deleting the object.

Persistent preparation remains available for another run:

```bash
sudo -n env DEMO_DATA_DIR="$DEMO_DATA_DIR" \
    bash "$PWD/kuasar-sandbox/test/demo/demo_prep.sh" stop
sudo -n env DEMO_DATA_DIR="$DEMO_DATA_DIR" \
    bash "$PWD/kuasar-sandbox/test/demo/demo_prep.sh" reset
```

`stop` stops only services identified by valid run-owned PID records and preserves data. `reset` first stops those services, then removes only an exactly marked, explicitly Demo-named data directory. Resolve any reported ambiguous external resource before resetting its diagnostics.


## 6. Network layout

Conductor listens on `127.0.0.1:443`; the independent Proxy listens on `127.0.0.2:443`. A local Demo CA signs `*.<domain>`, and SDK calls use its CA file plus `NO_PROXY=*`. `/etc/hosts` entries are added individually after each sandbox ID is known and removed by their per-run marker; wildcard DNS is not assumed.

The vSwitch assigns each sandbox a floating IP from `100.100.96.0/20`, while the E2B guest profile uses the reusable inner `169.254.0.21/30` address and `169.254.0.22` next hop. The run adds uniquely tagged forwarding and masquerade rules. The local Registry is exposed to builder MicroVMs through `--mgmt-service` on `169.254.169.254`. VersityGW stays on host loopback for the host-side COPY path; it does not use this guest route. Neither service is made externally reachable by the script.

The Demo verifies both direct `http://<floating-ip>:8000` access and the authenticated E2B data address `https://8000-<sid>.<domain>`, using the real `X-Access-Token`. Guest egress is an assertion of the existing Demo NAT path, not a new product Egress implementation.


## 7. Troubleshooting

A missing/wrong SDK or helper in release mode must be repaired by acquiring the
matching inputs and preparing a new workspace, not by global pip/PATH fallback.
Native source mode manages its own explicit virtual environment. Missing KVM or
kernel features must be resolved in the selected native environment; no emulation
or skipped assertion counts as success. For occupied resources, inspect the
instance owner rather than stopping unrelated services. For cleanup failure,
retain the reported private diagnostics and solve ownership before resetting.

See [Quick Start](../../docs/quickstart.md), [Release validation](../QUICKSTART.md),
[Node](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node.md) and
[Builder](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-build.md).
