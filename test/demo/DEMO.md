[English](DEMO.md) | [简体中文](DEMO_zh.md)

# E2B-compatible sandbox host — end-to-end demonstration

`demo_e2b.sh` exercises the full `orchestrator` path using the **unmodified E2B Python SDK** (`pip install e2b e2b-code-interpreter`): build a template with `Template().from_image()` (**image pulling and flattening happen inside a build-sandbox MicroVM, not through client-side docker build/push**), start a real MicroVM, execute guest commands, verify **port forwarding and outbound access**, pause/resume, **fan out new instances from a paused-state template**, perform **one-step migration** (import + resume), and destroy the sandbox.

The SDK is unchanged. Environment variables, local `/etc/hosts` entries and TLS issued by a local Demo CA (`SSL_CERT_FILE`) point it to this node, just as it would point to e2b.dev.

**Persistent storage is prepared once for this Demo:** `demo_prep.sh` starts the content store (`store-ctl`), local L1 cache (`cache-ctl`, tiered RocksDB) and image registry for **persistent reuse**. Store/cache listen on **Unix sockets**, not TCP ports. Data lives under `DEMO_DATA_DIR`, caching images and chunks across Demo runs. Each `demo_e2b.sh` invocation starts only orchestration and the eBPF switch. This is the Demo's selected storage setup, not a requirement that every Kuasar deployment use this data path.

<a id="演示了什么"></a>
## What the Demo demonstrates

| Step | Command (real E2B Python SDK / node-ctl) | Demonstrated behavior |
| --- | --- | --- |
| 1 | Start the orchestration stack | orchestrator with TLS, eBPF vSwitch and host NAT; existing store/cache reused over UDS. |
| 2 | `e2b-key-ctl` + `manifest-key add` | Credential model: ManifestKey protects content and pull tokens; a fixed KDF derives the default APISecret, which signs api_key; the pair is allowlisted in storage. |
| 3 | `Template().from_image(ref).build()` | Pull and flatten the image inside a **build-sandbox MicroVM**, using tenant credentials and networking; **no client-side docker build/push**. |
| 4 | `Sandbox.create(template)` | Cold-start a real Cloud Hypervisor MicroVM from the template, with guest envd ready. |
| 5 | `sbx.commands.run(…)` | Execute in the guest through proxy → envd, with default account `user` and writable `/home/user`. |
| 6 | `curl http://<floatingip>:port` / `https://<port>-<sid>.<domain>` | **Port forwarding** and sandbox **outbound NAT**. |
| 7 | `sbx.pause()` / `Sandbox.connect(id)` | Save a snapshot in the content store and resume it (**resume == connect**); data written before pause survives. |
| 8 | `export-sandbox --to-template` → `Sandbox.create(<tmpl>)` | Promote paused state to a remote template and fan out **new** sandboxes carrying that forked state. |
| 9 | `export-sandbox` → `Sandbox.connect(id, api_headers={migration-token})` | **One-step migration:** connect automatically imports and resumes. |
| 10 | `Sandbox.list()` / `sbx.kill()` | Lifecycle operations. |

<a id="前置条件"></a>
## Prerequisites

- Binaries (build from source with `make -C kuasar-sandbox build`; release packages include them): `node-ctl`, `store-ctl`, **`cache-ctl`** with CGO/RocksDB, `flatten-ctl`, `e2b-key-ctl`, `connector-ctl vswitch`, `cloud-hypervisor`, `vmlinux` and `sandbox-runtime.bundle`.
- Host: **systemd as PID 1 and root**, because orchestration uses D-Bus units, TLS port 443 and KVM; `/dev/kvm` must be readable/writable.
- Resources: the Demo build sandbox uses 2 vCPUs, 6 GiB capacity and 4 GiB allocatable. Leave additional host capacity for system services and running sandboxes.
- **E2B Python SDK:** `pip install e2b e2b-code-interpreter`.
- Tools: `python3`, `openssl`, `iproute2` (`ip`), `curl`, `sqlite3` and `iptables`. `demo_prep.sh` also needs `docker` to seed a base image into the registry once, and `zot` when no external registry is supplied.
- **Image registry:** set `REGISTRY=<host:port>`, with `REGISTRY_USER`/`REGISTRY_PASS`/`REGISTRY_INSECURE` as needed, for an external registry. Alternatively, leave `REGISTRY` unset and `demo_prep.sh` starts a **persistent local zot** listening only on `127.0.0.1`. Build sandboxes reach it through vSwitch `--mgmt-service`. The default base image is `e2bdev/code-interpreter:latest`; override it with `E2E_IMAGE=`.

<a id="运行"></a>
## Run

```bash
# Source workspace: run from the organization workspace root.
# 1. One-time persistent prerequisites: store/cache over UDS, registry and base-image seed.
#    Repeated invocation is idempotent and skips services already running.
REGISTRY=registry.example.com REGISTRY_USER=u REGISTRY_PASS=p bash kuasar-sandbox/test/demo/demo_prep.sh
bash kuasar-sandbox/test/demo/demo_prep.sh  # Or omit REGISTRY to start persistent local zot.
# Stop services: demo_prep.sh stop; stop and remove data: demo_prep.sh reset.

# 2. Per-run demonstration (reads ~/.cache/kuasar-demo/prep.env written by preparation).
sudo bash kuasar-sandbox/test/demo/demo_e2b.sh
sudo env DEMO_QUICKSTART=1 bash kuasar-sandbox/test/demo/demo_e2b.sh  # Stop after pause/resume and kill.
sudo env DEMO_PAUSE=1 bash kuasar-sandbox/test/demo/demo_e2b.sh       # Press Enter between steps.
sudo env DEMO_KEEP=1 bash kuasar-sandbox/test/demo/demo_e2b.sh        # Preserve this run's work directory.
sudo env DEMO_NETDIAG=1 bash kuasar-sandbox/test/demo/demo_e2b.sh     # Do not abort on networking-step failure.

# Release package: run from the extraction directory.
bash test/demo/demo_prep.sh
sudo bash test/demo/demo_e2b.sh
```

The main script can also be started by an ordinary user and re-enters through sudo automatically. The examples above use `sudo env` to pass Demo controls explicitly rather than relying on sudo preserving caller environment variables. `DEMO_QUICKSTART=1` skips template fan-out and migration after pause/resume and kill.

On exit, `demo_e2b.sh` cleans up the current orchestration units, vSwitch, NAT rules, temporary `/etc/hosts` entries and work directory. The **persistent store/cache/registry layer remains** for the next run.

<a id="网络配置端口转发--出网"></a>
## Network configuration: port forwarding and outbound access

The E2B-profile guest uses link-local **inner IP** `169.254.0.21/30` and default-route next hop `169.254.0.22`. The /30 and gateway allow envd port forwarding. All E2B sandboxes may use the same inner addresses; their floating IP identifies them on this Demo's host-facing path. The host and external network are not in that inner subnet.

The scripts automatically configure the following paths between host and sandbox, sandbox and host services, and sandbox and the external network:

```text
  host (root netns)                       vswitch (netns sw0)                 guest microVM
  curl <floatingip>:port ─route─► sw0m0 ─► ARP-proxy + DNAT floatingip->inner ─tap─► eth0 169.254.0.21/30
  reply ◄───────────────────────── SNAT inner->floatingip ◄──────── tap ◄──────────  http.server :port
                                                                                      (default via 169.254.0.22)

  egress: guest ─(default via 169.254.0.22)─► nx extract 0.0.0.0/0 ─► sw0m0
          ─► host NAT MASQUERADE (-s 100.100.96.0/20, ip_forward=1) ─► internet

  local registry: guest ─► 169.254.169.254:<port> ─mgmt-service─► 127.0.0.1:<port>
```

1. **`connector-ctl vswitch start … --mgmt-extract=:sw0m0:169.254.169.254,0.0.0.0/0`** creates management interface `sw0m0` in the host/root netns and adds route `100.100.96.0/20 dev sw0m0`. The CIDRs classify traffic; they are not interface addresses. The script separately assigns reserved address `169.254.1.0/31` to `sw0m0`, without hosting a service on it. eBPF answers ARP on the sw0m0 side, **DNATs** packets from host to floating IP into the sandbox inner IP and redirects them to the corresponding TAP. Replies are **SNATed** back to the floating IP. Including `0.0.0.0/0` in `mgmt_cidrs` provides the outbound path.
2. **Local service mapping:** `--mgmt-service=169.254.169.254:<port>:127.0.0.1:<port>` maps build-sandbox access to the management VIP onto zot bound only to host loopback. The script enables `route_localnet` on `sw0m0`. Optional MMDS uses the same mechanism for port 80.
3. **Host NAT**, idempotently added by the script and removed at exit: `iptables -A FORWARD -{i,o} sw0m0 …` and `-t nat -A POSTROUTING -s 100.100.96.0/20 -j MASQUERADE`.
4. **Guest inner IP, default route, `/etc/hosts` and `/etc/resolv.conf`** are supplied by orchestrator through SANDBOX_CONFIG `files:`. The hostname avoids getfqdn stalls; see the node specification, section 11. The Demo's iptables DNAT sends guest DNS `169.254.169.253` to the host's first nameserver.

Step 6 verifies two ways of accessing a sandbox port: **directly**, from the host through `sw0m0` to `http://<floatingip>:<port>`; and through the **E2B exposed-port address**, `https://<port>-<sid>.<domain>`, where the proxy validates `X-Access-Token` before forwarding to `floatingip:port`.

> **Per-sandbox `/etc/hosts` entries are enough for this SDK flow:** `Sandbox.create()` performs the control-plane POST and returns an object without immediately connecting to the data plane (the `mcp` mode is an exception). After create returns the sid, the Demo adds names such as `49983-<sid>.<domain>` to `/etc/hosts`; the first command then connects to envd. Wildcard DNS is not required. For immediate interaction with a sandbox you create yourself in another terminal, first add its host entry in the same way, or configure wildcard DNS, such as dnsmasq `address=/<domain>/127.0.0.1`. `/etc/hosts` does not support wildcards.

**Manual use in another terminal:** during tenant onboarding, the Demo writes SDK credentials to `/tmp/demo-e2b-cli-env.sh`. With `DEMO_PAUSE=1`, open another terminal and run `source /tmp/demo-e2b-cli-env.sh`, then use the SDK against this node, for example `python3 -c "from e2b import Sandbox; print([s.sandbox_id for s in Sandbox.list()])"`.

<a id="sdk-如何指向本节点零改造"></a>
## Pointing the unmodified SDK at this node

The E2B SDK derives control-plane address `https://api.<domain>` and data-plane address `https://<port>-<sid>.<domain>` from `E2B_DOMAIN`:

- `E2B_DOMAIN`/`E2B_API_KEY` point to this node. **Build directly with `from_image(<registry>/<image>)`:** pulling and flattening occur inside the build sandbox, using tenant-default or task-specific tokens (node specification, section 12). There is **no client-side docker build/push or `E2B_IMAGE_URI_MASK`**. The image reference must be **reachable from the build sandbox**. For local zot, the script rewrites the reference to the management VIP (`169.254.169.254:<port>`), mapped to `127.0.0.1:<port>` by `--mgmt-service`; external registries are reached through outbound NAT.
- The local Demo CA issues a `CA:FALSE` server certificate for `*.<domain>`. **`SSL_CERT_FILE=<ca.crt>`** lets the SDK verify TLS.
- `/etc/hosts` maps `api.<domain>` and each sandbox's `49983/49999/<port>-<sid>.<domain>` to `127.0.0.1`; sandbox entries are added after create and removed on exit.

<a id="说明与注意"></a>
## Notes and cautions

- **The base image must satisfy E2B userland conventions:** account `user`, `/bin/bash`, `util-linux` and `coreutils`. Envd executes commands as the default user through `ionice … nice …`. `e2bdev/code-interpreter` already supplies these. `demo_prep.sh` seeds it into the registry once; template `from_image` names it directly, and the build sandbox pulls it without a shim or rewrite.
- **Envd runs as root:** it is E2B infrastructure and needs root to setuid to the image's default user for workload commands. The E2B profile fixes `launch.user=0:0` instead of inheriting image `Config.User`.
- **Persistent storage reuse:** the preparation script keeps store/cache/registry running and stores data under `DEMO_DATA_DIR` (default `~/.cache/kuasar-demo`). Cached/deduplicated data is reused across runs. `demo_prep.sh reset` clears it for a fresh start.
- **TLS issued by the local Demo CA is for local demonstration only.** Production should use an appropriate trusted wildcard certificate for `*.<domain>`; see `orchestrator/docs/node.md`, section 13.

For automated assertion-based regression rather than an interactive walkthrough, see the assembled `test/e2e/orchestrator/` owner suite: `e2e_run_builder.sh` covers the three-stage build pipeline (guest pull/flatten → steps → template snapshot → create from the resulting template), and `e2e_execute.sh` covers startup, execution and state survival across pause/resume. In the source workspace these files are maintained by `orchestrator/test/e2e/`.
