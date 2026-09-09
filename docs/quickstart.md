[English](quickstart.md) | [简体中文](quickstart_zh.md)

# Quick Start

This guide downloads one published aggregate Release, verifies its complete asset set, and uses the unmodified E2B Python SDK to build a template, create a real MicroVM, execute commands, use the Files API, pause, reconnect/resume, and destroy the sandbox.

The commands resolve the Stable channel once and then pin every download to that concrete tag. To select a version explicitly, set `RELEASE_VERSION=release-vX.Y.Z` (or a published Preview tag) before starting. `releases/release.yaml` describes coordinated releases in source; it is not a “latest published” channel.

## 1. Check the host

Prebuilt assets target Linux x86_64 with glibc 2.38 or newer. The host needs systemd as PID 1, cgroup v2, root or non-interactive `sudo`, read/write `/dev/kvm`, at least 2 available vCPUs and 6 GiB for the builder plus capacity for the host and created sandbox.

On Ubuntu 24.04:

```bash
sudo apt-get update
sudo apt-get install -y \
    ca-certificates curl docker.io e2fsprogs iproute2 iptables \
    libgcc-s1 liblz4-1 libsnappy1v5 libstdc++6 libzstd1 openssl \
    procps python3 python3-venv sqlite3 tar util-linux zlib1g
```

Run the fail-fast preflight. The Demo deliberately does not change the host-global forwarding setting; enable it according to the host's network policy before continuing.

```bash
(
set -e
test "$(uname -s)" = Linux
test "$(uname -m)" = x86_64
test "$(ps -p 1 -o comm=)" = systemd
test -f /sys/fs/cgroup/cgroup.controllers
test -c /dev/kvm
sudo -n test -r /dev/kvm
sudo -n test -w /dev/kvm
sudo -n true
test "$(cat /proc/sys/net/ipv4/ip_forward)" = 1
glibc_version="$(getconf GNU_LIBC_VERSION | awk '{print $2}')"
test "$(printf '%s\n' 2.38 "$glibc_version" | sort -V | sed -n '1p')" = 2.38
for tool in tar sha256sum python3 openssl ip curl sqlite3 iptables ss \
    mkfs.ext4 docker ldd flock setsid timeout; do
    command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done
sudo -n docker info >/dev/null
)
```

No GitHub account, `gh` login, or organization access is part of normal public download and use. Docker is used only to run a local Registry and seed its base image; it is not a runtime dependency of every deployment.

## 2. Resolve one Release and download its exact assets

Run this block in one shell. It accepts only the aggregate naming contract, verifies Stable/Preview metadata, and records the exact asset URLs, GitHub-provided digests, and sizes from one Release object.

```bash
set -euo pipefail
RELEASE_VERSION="${RELEASE_VERSION:-}"
RELEASE_METADATA="$(mktemp)"
ASSETS_TSV="$(mktemp)"
trap 'rm -f -- "$RELEASE_METADATA" "$ASSETS_TSV"' EXIT

if [ -n "$RELEASE_VERSION" ]; then
    [[ "$RELEASE_VERSION" =~ ^release-v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-preview\.[0-9]{8})?$ ]]
    RELEASE_API="https://api.github.com/repos/kuasar-sandbox/kuasar-sandbox/releases/tags/$RELEASE_VERSION"
else
    RELEASE_API="https://api.github.com/repos/kuasar-sandbox/kuasar-sandbox/releases/latest"
fi
curl --fail --silent --show-error --location --retry 4 \
    -H 'Accept: application/vnd.github+json' \
    -H 'X-GitHub-Api-Version: 2022-11-28' \
    "$RELEASE_API" >"$RELEASE_METADATA"

RELEASE_VERSION="$(python3 - "$RELEASE_METADATA" "$RELEASE_VERSION" "$ASSETS_TSV" <<'PY'
import json, re, sys
metadata, requested, output = sys.argv[1:]
release = json.load(open(metadata, encoding="utf-8"))
tag = release.get("tag_name", "")
tag_re = re.compile(r"^release-v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-preview\.[0-9]{8})?$")
assert tag_re.fullmatch(tag), tag
assert not release.get("draft")
assert bool(re.search(r"-preview\.[0-9]{8}$", tag)) == bool(release.get("prerelease"))
assert not requested or requested == tag, (requested, tag)
patterns = {
    "platform": re.compile(rf"^platform-{re.escape(tag)}\.tar\.gz$"),
    "accelerator": re.compile(r"^accelerator-v[^/]+-linux-x86_64\.tar\.gz$"),
    "connector": re.compile(r"^connector-v[^/]+-linux-x86_64\.tar\.gz$"),
    "orchestrator": re.compile(r"^orchestrator-v[^/]+-linux-x86_64\.tar\.gz$"),
    "sandboxer": re.compile(r"^sandboxer-v[^/]+-linux-x86_64\.tar\.gz$"),
    "runtime": re.compile(r"^sandbox-runtime-x86_64-v[^/]+\.tar\.gz$"),
    "vmlinux": re.compile(r"^vmlinux-x86_64-v[^/]+\.tar\.gz$"),
}
assets = release.get("assets", [])
assert len(assets) == 8, [a.get("name") for a in assets]
names = [a.get("name", "") for a in assets]
assert names.count("SHA256SUMS") == 1
for label, pattern in patterns.items():
    matches = [name for name in names if pattern.fullmatch(name)]
    assert len(matches) == 1, (label, matches)
prefix = f"https://github.com/kuasar-sandbox/kuasar-sandbox/releases/download/{tag}/"
with open(output, "w", encoding="utf-8") as stream:
    for asset in sorted(assets, key=lambda item: item["name"]):
        name, url, digest, size = (asset.get(k) for k in ("name", "browser_download_url", "digest", "size"))
        assert re.fullmatch(r"[A-Za-z0-9._-]+", name), name
        assert url == prefix + name, url
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest or ""), (name, digest)
        assert isinstance(size, int) and size > 0, (name, size)
        stream.write(f"{name}\t{url}\t{digest}\t{size}\n")
print(tag)
PY
)"

DOWNLOAD_DIR="$PWD/kuasar-download-$RELEASE_VERSION"
INSTALL_DIR="$PWD/kuasar-$RELEASE_VERSION"
test ! -e "$DOWNLOAD_DIR"
test ! -e "$INSTALL_DIR"
mkdir -m 0755 "$DOWNLOAD_DIR"
install -m 0644 "$RELEASE_METADATA" "$DOWNLOAD_DIR/release.json"
install -m 0644 "$ASSETS_TSV" "$DOWNLOAD_DIR/assets.tsv"

while IFS=$'\t' read -r name url digest size; do
    curl --fail --silent --show-error --location --retry 4 \
        --output "$DOWNLOAD_DIR/$name.part" "$url"
    test "$(stat -c %s "$DOWNLOAD_DIR/$name.part")" = "$size"
    test "sha256:$(sha256sum "$DOWNLOAD_DIR/$name.part" | awk '{print $1}')" = "$digest"
    mv "$DOWNLOAD_DIR/$name.part" "$DOWNLOAD_DIR/$name"
done <"$DOWNLOAD_DIR/assets.tsv"
rm -f -- "$RELEASE_METADATA" "$ASSETS_TSV"
trap - EXIT
printf 'Pinned aggregate Release: %s\n' "$RELEASE_VERSION"
```

This does not use moving asset URLs after resolution. All eight explicit assets are obtained from the same concrete aggregate Release: one platform archive, six component release-unit archives, and `SHA256SUMS`.

## 3. Verify and extract without path collisions

The following validation checks `SHA256SUMS`, rejects unsupported tar entry types, absolute/traversing/noncanonical names, unsafe file modes, and file/directory or cross-archive collisions before extracting into a fresh staging directory.

```bash
set -euo pipefail
(
cd "$DOWNLOAD_DIR"
sha256sum --quiet --check SHA256SUMS
)

python3 - "$DOWNLOAD_DIR" <<'PY'
import pathlib, re, sys, tarfile
root = pathlib.Path(sys.argv[1])
rows = [line.split("\t") for line in (root / "assets.tsv").read_text().splitlines()]
archives = [name for name, *_ in rows if name.endswith(".tar.gz")]
checksum_re = re.compile(r"^([0-9a-f]{64}) [ *]([A-Za-z0-9._-]+)$")
checksums = {}
for line in (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
    match = checksum_re.fullmatch(line)
    assert match, line
    digest, name = match.groups()
    assert name not in checksums
    checksums[name] = digest
assert set(checksums) == set(archives), (sorted(checksums), sorted(archives))
types = {}
for archive in archives:
    with tarfile.open(root / archive, "r:gz") as stream:
        for member in stream.getmembers():
            raw = member.name
            while raw.startswith("./"):
                raw = raw[2:]
            raw = raw.rstrip("/")
            if not raw:
                assert member.isdir(), (archive, member.name)
                continue
            assert "\\" not in raw and not any(ord(c) < 32 for c in raw), (archive, raw)
            path = pathlib.PurePosixPath(raw)
            assert not path.is_absolute() and ".." not in path.parts and "." not in path.parts
            assert "/".join(path.parts) == raw, (archive, raw)
            kind = "dir" if member.isdir() else "file" if member.isfile() else "unsupported"
            assert kind != "unsupported", (archive, raw, member.type)
            assert not (member.mode & 0o6000), (archive, raw, oct(member.mode))
            assert kind == "dir" or not (member.mode & 0o002), (archive, raw, oct(member.mode))
            prior = types.get(raw)
            assert prior is None or prior == kind == "dir", (archive, raw, prior, kind)
            for parent in path.parents:
                if str(parent) != ".":
                    assert types.get(str(parent)) != "file", (archive, raw, str(parent))
            if kind == "file":
                assert not any(existing.startswith(raw + "/") for existing in types), (archive, raw)
            types[raw] = kind
PY

INSTALL_PARENT="$(dirname "$INSTALL_DIR")"
STAGE_DIR="$(mktemp -d "$INSTALL_PARENT/.kuasar-install.XXXXXX")"
cleanup_stage() {
    case "$STAGE_DIR" in "$INSTALL_PARENT"/.kuasar-install.*) rm -rf -- "$STAGE_DIR" ;; esac
}
trap cleanup_stage EXIT
while IFS=$'\t' read -r name _; do
    case "$name" in
        *.tar.gz) tar --extract --gzip --no-same-owner --no-same-permissions \
            --file "$DOWNLOAD_DIR/$name" --directory "$STAGE_DIR" ;;
    esac
done <"$DOWNLOAD_DIR/assets.tsv"

# This marker prevents mixing the corrected current guide with an older
# archive whose Demo has a different configuration and cleanup contract.
test -f "$STAGE_DIR/test/demo/demo_common.sh" || {
    echo "selected Release predates the current safe Demo contract; use its bundled guide for historical reproduction or use source mode" >&2
    exit 1
}
test -f "$STAGE_DIR/test/demo/requirements.txt"
test -d "$STAGE_DIR/bin" && test -d "$STAGE_DIR/docs" && test -d "$STAGE_DIR/test"
for executable in "$STAGE_DIR/bin/cache-ctl" "$STAGE_DIR/bin/cloud-hypervisor"; do
    output="$(ldd "$executable" 2>&1)" || { printf '%s\n' "$output" >&2; exit 1; }
    ! grep -q 'not found' <<<"$output" || { printf '%s\n' "$output" >&2; exit 1; }
done
mv "$STAGE_DIR" "$INSTALL_DIR"
STAGE_DIR=""
trap - EXIT
```

If the channel still points to a Release that predates the corrected Demo contract, the marker check stops safely. Existing Release assets are immutable; use the source-mode path in section 7 for the current implementation instead of mixing new scripts with that old binary set.

## 4. Install the SDK and start a run-owned Registry

Use the exact SDK requirement shipped with the selected scripts. The local Registry image below is pinned to the Linux amd64 manifest of the Docker Official Image; the random container ID and ownership label avoid a shared fixed name.

```bash
python3 -m venv "$INSTALL_DIR/.venv"
PYTHON_BIN="$INSTALL_DIR/.venv/bin/python3"
"$PYTHON_BIN" -m pip install --requirement "$INSTALL_DIR/test/demo/requirements.txt"

RUN_ID="$(tr -d '-' </proc/sys/kernel/random/uuid | cut -c1-10)"
DEMO_DATA_DIR="/var/lib/kuasar-demo-quickstart-$RUN_ID"
BIN="$INSTALL_DIR/bin"
REGISTRY_IMAGE='docker.io/library/registry@sha256:46faa9a1ae6813194b53921a370f2f4f8c5e1aae228a89bceafef5847a6a3278'
if [[ -n "$(sudo -n ss -H -ltn 'sport = :5000')" ]]; then
    echo 'port 5000 is already in use; refusing to alter its owner' >&2
    exit 1
fi
REGISTRY_CID="$(sudo -n docker run -d --network host \
    --label "io.kuasar-sandbox.demo-run=$RUN_ID" \
    -e REGISTRY_HTTP_ADDR=127.0.0.1:5000 \
    "$REGISTRY_IMAGE")"
test "$(sudo -n docker inspect --format '{{ index .Config.Labels "io.kuasar-sandbox.demo-run" }}' "$REGISTRY_CID")" = "$RUN_ID"
REGISTRY_READY=0
for _ in $(seq 1 40); do
    if curl --fail --silent --noproxy '*' http://127.0.0.1:5000/v2/ >/dev/null; then
        REGISTRY_READY=1
        break
    fi
    sleep 0.25
done
if [[ "$REGISTRY_READY" != 1 ]]; then
    if [[ "$(sudo -n docker inspect --format '{{ index .Config.Labels "io.kuasar-sandbox.demo-run" }}' "$REGISTRY_CID" 2>/dev/null || true)" == "$RUN_ID" ]]; then
        sudo -n docker rm -f "$REGISTRY_CID" >/dev/null
    else
        echo "Registry ownership changed; preserving $REGISTRY_CID for inspection" >&2
    fi
    echo 'run-owned Registry did not become ready' >&2
    exit 1
fi
```

If port 5000 is already occupied, stop and identify its owner; do not remove an existing container or listener. An operator-supplied Registry may be used instead, including credentials passed as documented in the [Demo](../test/demo/DEMO.md).

## 5. Prepare, build, create, access, pause, resume, and destroy

All values crossing `sudo` are explicit. `DEMO_DATA_DIR` is root-owned and private; `PYTHON_BIN` remains the absolute interpreter from the user-created virtual environment.

```bash
sudo -n env DEMO_DATA_DIR="$DEMO_DATA_DIR" BIN="$BIN" \
    REGISTRY=127.0.0.1:5000 REGISTRY_INSECURE=1 \
    bash "$INSTALL_DIR/test/demo/demo_prep.sh"

sudo -n env DEMO_DATA_DIR="$DEMO_DATA_DIR" BIN="$BIN" \
    PYTHON_BIN="$PYTHON_BIN" DEMO_QUICKSTART=1 \
    bash "$INSTALL_DIR/test/demo/demo_e2b.sh"
```

The first preparation pulls a digest-pinned base image and pushes it under a digest-derived destination tag, so duration depends on network and storage. It reads the destination back and refuses to overwrite different content.

Quick Start succeeds only after the script has built and read back an `snp` template target, created a real MicroVM, executed guest commands, written and read through the Files API, verified direct and authenticated data access plus outbound NAT, preserved both data forms across pause/resume, called `kill()`, and completed run-owned cleanup.

## 6. Clean up

Stop persistent Demo services first. `stop` preserves cached data; `reset` removes only the exactly marked Demo directory after stopping its services.

```bash
sudo -n env DEMO_DATA_DIR="$DEMO_DATA_DIR" \
    bash "$INSTALL_DIR/test/demo/demo_prep.sh" stop
# Or, after inspecting any reported incomplete cleanup:
sudo -n env DEMO_DATA_DIR="$DEMO_DATA_DIR" \
    bash "$INSTALL_DIR/test/demo/demo_prep.sh" reset

test "$(sudo -n docker inspect --format '{{ index .Config.Labels "io.kuasar-sandbox.demo-run" }}' "$REGISTRY_CID")" = "$RUN_ID"
sudo -n docker rm -f "$REGISTRY_CID"
```

The label readback is required before removing the container. The Demo never wildcard-stops all sandbox units and never deletes a pre-existing vSwitch or namespace. If it reports ambiguous ownership or incomplete cleanup, the run is failed and its root-only diagnostics are preserved for inspection.

## 7. Current source mode

For development, or while the Stable channel predates the corrected Demo contract, clone the six public repositories as siblings, record their exact SHAs, build that source set, and follow the source-mode commands in the [Demo](../test/demo/DEMO.md). Do not use component GitHub Latest independently; compatible release compositions are selected by the aggregate Release.

The project root is not a monorepo:

```text
<workspace>/
├── kuasar-sandbox/
├── accelerator/
├── connector/
├── guest-runtime/
├── sandboxer/
└── orchestrator/
```

## 8. Troubleshooting and next steps

- `systemd is not PID1`: use a systemd-booted Linux host, not an ordinary container.
- `/dev/kvm not available (rw)`: enable virtualization and make the device available to root.
- `net.ipv4.ip_forward must already be 1`: configure forwarding according to the host's policy; the Demo intentionally does not alter it.
- `selected Release predates ...`: do not fetch scripts from `main` for those binaries. Use its bundled historical guide or build one exact current source set.
- A Registry, listener, unit, vSwitch, namespace, link, host mapping, or iptables conflict is intentionally left untouched.
- `DEMO_NETDIAG=1` is diagnostic only and cannot turn a failed networking assertion into acceptance evidence.

The complete Demo adds `COPY`, paused-state template fan-out, and migration. See [Demo](../test/demo/DEMO.md), [Deployment](deployment.md), [Releases](release.md), and the [Security Policy](../SECURITY.md).
