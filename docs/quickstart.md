[English](quickstart.md) | [简体中文](quickstart_zh.md)

# Run your first Guest

Start a persistent node with the published **node-infra** image, run a command in
an actual MicroVM, and inspect or stop the node. This first run needs neither a
source build nor Workbench, a separate Store/Cache service, a container-image
registry inside the Guest, or a hand-created overlay filesystem.

The `quickstart` profile supplies a small Linux shell filesystem and private
credentials. It is a local trial: public API/data ports are not exposed, ordinary Guest
Internet access is not configured, and it supplies the shipped shell workload
rather than a complete site-specific Build/SDK setup. For your own
images, E2B SDK clients or a cluster, continue with the explicit site deployment
below. The full [SDK Demo](../test/demo/DEMO.md) remains available for development
and release validation.

## 1. Select a matching published image

Use native Linux x86_64 or aarch64 with a local rootful Docker Engine, cgroup v2,
usable `/dev/kvm` and `/dev/net/tun`, network namespaces, BPF/TC and userfaultfd.
The [host requirements](node-infra.md#prerequisites-and-release-inputs) cover the
system-container path. Docker Desktop, architecture emulation and a remote
Docker daemon do not substitute for these host capabilities. On WSL 2, first
check the [Linux host and resource requirements](../workbench/README.md#windows-and-wsl-2-development-hosts).

Choose one published aggregate tag from
[Releases](https://github.com/kuasar-sandbox/kuasar-sandbox/releases) and replace
`<aggregate-tag>` below. Use its matching packaged guide and keep the exact tag
for later runs. [Installation](installation.md) covers offline archives and
separately operated native components.

```bash
RELEASE_VERSION='<aggregate-tag>'
IMAGE="ghcr.io/kuasar-sandbox/node-infra:$RELEASE_VERSION"
docker pull "$IMAGE"
```

Record the release and the image identity reported by `docker pull`. The node-infra image tag includes
`release-`. For offline use, verify and load the matching `.docker.tar.gz` archive
as described in [node-infra inputs](node-infra.md#prerequisites-and-release-inputs),
then set `IMAGE` to the loaded image ID. Docker imports the Docker archive;
Podman can also import the separate OCI archive.

## 2. Start the node and run a command

Use an unused container name and a new dedicated volume. The example reserves
4 CPUs and 6 GiB for the whole node, including its management services and Guest;
it is a trial budget, not a general sizing recommendation.

```bash
docker run -d --tty --name ks-dev --pull=never --privileged \
  --cgroupns=private --cpus=4 --memory=6g --memory-swap=6g \
  --tmpfs /run --tmpfs /run/lock --tmpfs /tmp:exec --stop-timeout=420 \
  --network=bridge -v ks-dev-data:/var/lib/node-infra "$IMAGE" quickstart
docker exec ks-dev node-infra-run -- /bin/sh -c 'printf "hello from guest\n"'
```

Expect `hello from guest` and exit status zero. `node-infra-run` waits for node
initialization and an idle Runner, creates a fresh MicroVM, executes the argument
vector inside that Guest, returns its output and exit status, and deletes it.
Startup, control-request or cleanup failures return a nonzero status even if the
Guest command completed. The printed sandbox ID identifies that one run. Repeat the second command for
another independent Guest; files written by one invocation are not the next
invocation's retained session.

While initialization, capacity or Proxy readiness is pending, the helper prints
one waiting message per stage to stderr. Guest stdout and its exit status remain
available to scripts. To use an interactive shell, or supply standard input:

```bash
docker exec -it ks-dev node-infra-run -- /bin/sh
printf 'input to guest\n' | docker exec -i ks-dev node-infra-run -- /bin/cat
docker exec ks-dev node-infra-run --help
```

`exit` ends the interactive shell and deletes that Guest. Use `-i` for piped
input and `-it` for a terminal. The shell and piped command each use a separate Guest. `--help` only displays usage.

The node stays running. Its dedicated volume retains node identity, credentials
and referenced base Bundles. The profile creates these once from shipped inputs;
it does not download a workload image at startup. Mount the volume directly at
`/var/lib/node-infra` without nested mounts. Keep separate volumes for separate
nodes, and retain their referenced artifacts for recovery.

## 3. Inspect, stop and restart

```bash
docker exec ks-dev /usr/libexec/node-infra/node-infra-runtime health
docker exec ks-dev node-ctl --version
docker inspect --format '{{.Image}}' ks-dev
docker logs ks-dev
docker exec ks-dev journalctl -u node-conductor.service --no-pager

docker stop ks-dev
docker start ks-dev
docker exec ks-dev node-infra-run -- /bin/sh -c 'printf "guest after restart\n"'
```

Stopping the node ends running VMs; it does not automatically capture every
Guest's memory. Restarting reuses the node's durable inputs. If the command client
is killed with SIGKILL, it cannot send Delete: retain its printed sandbox ID and
inspect/delete it through the node lifecycle API. Ordinary completion performs
that cleanup itself.

To finish the trial, stop and remove the container. Keep the volume while you
need its data or logs. Delete it only after deciding to discard that trial:

```bash
docker stop ks-dev
docker rm ks-dev
# Optional: permanently discard this trial's persistent data.
docker volume rm ks-dev-data
```

## If a step fails

| Symptom | Next check |
| --- | --- |
| Container or preflight fails | Host KVM/TUN, cgroup v2 delegation, kernel capabilities and `docker logs`; use a capable native host. |
| Profile rejects configuration or data | Use a dedicated empty volume for first initialization; do not mount site `/etc` configuration into this profile or erase an existing node's data to make it pass. |
| Guest command waits or fails | Internal health and Conductor/Runner journal; confirm node capacity and preserve the first error before retrying. |
| Guest cannot reach the Internet | Expected for this profile. Docker bridge access is the node's network, not Guest SNAT. Configure an explicit gateway in a site deployment. |

## Continue with your own application

Create a separate [site deployment](deployment.md#production-start) using the
same selected release's image and deployment package. Configure API/data ingress,
credentials, artifact retention, Runner/Builder pools and optional
[native egress](node-infra.md#native-egress). Omit the `quickstart` argument and
supply the native configuration paths. The trial profile rejects site configuration
mounts; adding overlays to it is not an in-place application deployment procedure.

Then [build a template](templates.md) and [use the E2B SDK](sandboxes.md). For
multiple nodes follow the [minimum cluster](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/cluster.md#minimum-cluster)
and keep Group/route identity with each instance. Workbench's
[full validation workflow](../test/QUICKSTART.md) is a separate task for qualifying
those deployment choices.
