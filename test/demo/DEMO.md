[English](DEMO.md) | [简体中文](DEMO_zh.md)

# E2B-compatible sandbox host Demo

This Demo drives a standalone Kuasar Sandbox node through the unmodified E2B Python SDK. It uses the current separate Conductor and Proxy processes, builds a snapshot template inside a builder MicroVM, creates a real MicroVM, exercises command and Files APIs, verifies data access, pauses and resumes the logical sandbox, and destroys it. The complete mode also covers template fan-out and one-call migration.

The Demo is an executable product path, not a replacement for the component and aggregate Integration E2E suites. A run without writable KVM, or a run in `DEMO_NETDIAG` mode that tolerates a failed network assertion, is not acceptance evidence.

## 1. Execution modes

Use one internally consistent source set:

- **Source mode:** build all six sibling repositories with the project `Makefile`, then run the scripts from that same source revision against the resulting `bin/<arch>` directory.
- **Release mode:** resolve one aggregate Release tag, verify every asset from that Release, extract it, and use both the scripts and binaries from that extraction. Do not combine scripts from `main` with binaries from an older Stable Release.

`demo_prep.sh` owns the persistent Demo layer: Manifest Store, tiered Cache, Registry configuration, immutable base-image seed, and optional VersityGW storage used by `COPY`. `demo_e2b.sh` owns one ephemeral run: TLS, credentials, Conductor, Proxy, systemd units, vSwitch, network namespace, NAT rules, host mappings, sandboxes, and private work files.

COPY storage stays reachable from the host: Conductor performs HEAD/presigning,
the SDK uploads directly, and the Builder downloads the context before streaming
it into the build VM. Registry image import is different: it runs in the guest
and uses the Demo's management-VIP route to local Zot.

## 2. What is verified

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
| Fan-out | `export-sandbox --to-template`, then `Sandbox.create` | A listed new child returns the asserted command-written and Files-API data |
| Migration | `Sandbox.connect(id, headers={migration-token})` | Import and resume complete in one SDK call; both data values are asserted again |
| Destroy | `Sandbox.kill` and run-owned cleanup | Sandboxes and all safely attributable ephemeral host resources are gone |

Quick Start mode (`DEMO_QUICKSTART=1`) stops after build, create, execution/data access, pause/resume, and kill. Complete mode requires VersityGW and continues through fan-out and migration.

The SDK forwards `Template.build(headers=...)` to both registration and trigger,
but Kuasar Builder configuration is register-only. The Demo therefore leaves the
target automatic and supplies start/ready commands, then strictly checks the
snapshot result. The pinned SDK returns its registration handle after waiting;
the Demo reads the published, creatable template ID from that exact build's status.

## 3. Host and tool prerequisites

The supported prebuilt target is Linux x86_64 with glibc 2.38 or newer. A real run also requires:

- systemd as PID 1, cgroup v2, root, and read/write `/dev/kvm`;
- `net.ipv4.ip_forward=1`, enabled by the operator before the run; the Demo refuses to mutate this host-global setting;
- free `127.0.0.1:443` and `127.0.0.2:443`, no standard `sandbox-runner@*` or `sandbox-builder@*` instances, and no conflicting Demo vSwitch, namespace, link, unit, host entry, or iptables marker;
- `openssl`, `ip`, `curl`, `sqlite3`, `iptables`, `flock`, `setsid`, `timeout`, `mkfs.ext4`, and Docker for seeding an OCI Registry;
- the exact Python dependency in [`requirements.txt`](requirements.txt), installed in a dedicated virtual environment;
- `node-ctl`, `e2b-key-ctl`, `connector-ctl`, `store-ctl`, `cache-ctl`, `cloud-hypervisor`, `vmlinux`, and `sandbox-runtime.bundle` from one source set.

The complete mode additionally needs `versitygw`. Source mode obtains pinned Zot and VersityGW tools through `make e2e-tools`. Release mode can use an operator-supplied Registry and VersityGW; the [Quick Start](../../docs/quickstart.md) gives a complete release-first path for the shorter mode.

The build sandbox requests 2 vCPUs and 6 GiB capacity. Leave memory and CPU for the host and the sandbox created from the template.

## 4. Source-mode run

Run from the parent directory that contains the six sibling repositories:

```bash
make -C kuasar-sandbox build e2e-tools
python3 -m venv kuasar-sandbox/.demo-venv
kuasar-sandbox/.demo-venv/bin/python3 -m pip install \
    --requirement kuasar-sandbox/test/demo/requirements.txt

DEMO_DATA_DIR=/var/lib/kuasar-demo-source
BIN="$PWD/kuasar-sandbox/bin/x86_64"
PYTHON_BIN="$PWD/kuasar-sandbox/.demo-venv/bin/python3"
ZOT_BIN="$PWD/kuasar-sandbox/build/e2e-tools/x86_64/zot"
VGW_BIN="$PWD/kuasar-sandbox/build/e2e-tools/x86_64/versitygw"

sudo -n env DEMO_DATA_DIR="$DEMO_DATA_DIR" BIN="$BIN" \
    ZOT_BIN="$ZOT_BIN" VGW_BIN="$VGW_BIN" \
    bash "$PWD/kuasar-sandbox/test/demo/demo_prep.sh"
sudo -n env DEMO_DATA_DIR="$DEMO_DATA_DIR" BIN="$BIN" \
    PYTHON_BIN="$PYTHON_BIN" \
    bash "$PWD/kuasar-sandbox/test/demo/demo_e2b.sh"
```

All paths crossing `sudo` are explicit and absolute. The scripts do not rely on the caller and root having the same `HOME` or Python installation.

The default base is the compact Docker Official `python:3.12-slim` linux/amd64 manifest pinned by digest. The template build uses `set_user("root")` to create the E2B default `user` account (UID/GID 1000:1000), then `set_user("user")` before COPY and snapshot startup; an existing account must already use those IDs. These are Builder USER steps, not the SDK's optional `run_cmd(user=...)` argument. Its Python runtime is sufficient for readiness, execution, and data-access checks. An overridden image must also provide `useradd` when the account is absent. To use an existing Registry instead of the Demo-owned Zot, pass `REGISTRY=<host[:port]>`. If authentication is required, pass `REGISTRY_USER` and `REGISTRY_PASS`; `demo_prep.sh` sends the password to `docker login` on stdin and stores Docker authentication only below the private Demo data directory. Set `REGISTRY_INSECURE=1` only for an intentionally HTTP Registry. Every `E2E_IMAGE` override must be an immutable `name@sha256:<digest>` reference.

## 5. Controls and repeated runs

```bash
# Short lifecycle only.
sudo -n env DEMO_DATA_DIR="$DEMO_DATA_DIR" BIN="$BIN" \
    PYTHON_BIN="$PYTHON_BIN" DEMO_QUICKSTART=1 \
    bash "$PWD/kuasar-sandbox/test/demo/demo_e2b.sh"

# Pause between phases; a private root-only cli.env is printed for a second root terminal.
sudo -n env DEMO_DATA_DIR="$DEMO_DATA_DIR" BIN="$BIN" \
    PYTHON_BIN="$PYTHON_BIN" DEMO_PAUSE=1 \
    bash "$PWD/kuasar-sandbox/test/demo/demo_e2b.sh"

# Retain only logs that do not contain detected run secrets.
sudo -n env DEMO_DATA_DIR="$DEMO_DATA_DIR" BIN="$BIN" \
    PYTHON_BIN="$PYTHON_BIN" DEMO_KEEP=1 \
    bash "$PWD/kuasar-sandbox/test/demo/demo_e2b.sh"
```

Preparation is idempotent only when the live services still match their recorded executable, start time, and configuration. A repeated per-run invocation gets a fresh random identity. `DEMO_RUN_ID` may be supplied for reproducibility, but an existing work/result directory or host marker is treated as a conflict, not adopted.

`DEMO_NETDIAG=1` keeps a diagnostic run going after direct-port or egress assertions fail. Do not use such a run as successful Demo or Release validation.

## 6. Ownership, credentials, and cleanup

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

## 7. Network and endpoint layout

Conductor listens on `127.0.0.1:443`; the independent Proxy listens on `127.0.0.2:443`. A local Demo CA signs `*.<domain>`, and SDK calls use its CA file plus `NO_PROXY=*`. `/etc/hosts` entries are added individually after each sandbox ID is known and removed by their per-run marker; wildcard DNS is not assumed.

The vSwitch assigns each sandbox a floating IP from `100.100.96.0/20`, while the E2B guest profile uses the reusable inner `169.254.0.21/30` address and `169.254.0.22` next hop. The run adds uniquely tagged forwarding and masquerade rules. The local Registry is exposed to builder MicroVMs through `--mgmt-service` on `169.254.169.254`. VersityGW stays on host loopback for the host-side COPY path; it does not use this guest route. Neither service is made externally reachable by the script.

The Demo verifies both direct `http://<floating-ip>:8000` access and the authenticated E2B data address `https://8000-<sid>.<domain>`, using the real `X-Access-Token`. Guest egress is an assertion of the existing Demo NAT path, not a new product Egress implementation.

## 8. Troubleshooting and See Also

- A missing or mismatched Python package must be fixed in the dedicated virtual environment; the script requires exactly `e2b==2.25.1`.
- A listener, unit, vSwitch, namespace, link, host mapping, or iptables conflict is intentionally not auto-removed. Use an unused host or resolve the named owner outside the Demo.
- On first preparation, pulling and pushing the digest-pinned base image may take substantial time. A failed destination pull is considered “missing” only when the Registry clearly reports an absent manifest.
- A `cleanup was incomplete` message is a failed run. Inspect the preserved root-only directory and named host object before retrying.

See [Quick Start](../../docs/quickstart.md), [Standalone node design](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node.md), [Builder design](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-build.md), and [Integration E2E guide](../QUICKSTART.md).
