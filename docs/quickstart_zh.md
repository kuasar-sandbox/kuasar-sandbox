[English](quickstart.md) | [简体中文](quickstart_zh.md)

# Quick Start

本指南下载一个已经发布的聚合 Release,校验其完整资产集合,再通过未修改的 E2B Python SDK 构建 Template,创建真实 MicroVM,执行命令,使用 Files API,暂停,重新连接/恢复并销毁 Sandbox。

命令只解析一次 Stable 渠道,随后把全部下载固定到该具体 Tag。如需显式选择版本,在开始前设置 `RELEASE_VERSION=release-vX.Y.Z`(也可使用已发布的 Preview Tag)。源码中的 `releases/release.yaml` 描述协调发布,不是“最新已发布版本”渠道。

## 1. 检查主机

预构建资产面向 Linux x86_64,需要 glibc 2.38 或更高版本。主机需要 systemd 作为 PID 1、cgroup v2、root 或无交互 `sudo`、可读写 `/dev/kvm`,还要为 Builder 提供至少 2 个可用 vCPU 和 6 GiB 内存,并为 host 与创建后的 Sandbox 保留容量。

Ubuntu 24.04 可执行:

```bash
sudo apt-get update
sudo apt-get install -y \
    ca-certificates curl docker.io e2fsprogs iproute2 iptables \
    libgcc-s1 liblz4-1 libsnappy1v5 libstdc++6 libzstd1 openssl \
    procps python3 python3-venv sqlite3 tar util-linux zlib1g
```

运行 fail-fast 前置检查。Demo 不会修改 host-global forwarding 设置;继续之前应由操作者根据主机网络策略启用该设置。

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

正常公共下载和使用不需要 GitHub 账号、`gh` 登录或组织访问权限。Docker 只用于运行本地 Registry 并 seed 基础镜像,不是所有部署的 Runtime 依赖。

## 2. 解析一个 Release 并下载其精确资产

在同一 shell 中运行以下代码。它只接受聚合版本命名合同,校验 Stable/Preview 元数据,并从一个 Release 对象记录精确资产 URL、GitHub 提供的 digest 与 size。

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

解析完成后不再使用移动资产 URL。同一具体聚合 Release 提供全部八个显式资产:一个 platform archive、六个组件发行单元 archive 和 `SHA256SUMS`。

## 3. 校验并以无路径冲突方式解包

以下验证先检查 `SHA256SUMS`,再拒绝不支持的 tar entry type、absolute/traversal/noncanonical name、不安全文件 mode,以及 file/directory 或跨 archive collision;全部通过后才解包到新的 staging directory。

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

# 此 marker 防止把当前修正后的指南与使用不同配置/清理合同的旧 Demo 混用。
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

如果 Stable 渠道仍指向早于修正后 Demo 合同的 Release,marker 检查会安全停止。已有 Release 资产不可变;应使用第 7 节源码模式运行当前实现,不能把新脚本与旧 binary set 混用。

## 4. 安装 SDK 并启动运行所属 Registry

使用所选脚本交付的精确 SDK requirement。以下本地 Registry image 固定到 Docker Official Image 的 Linux amd64 manifest;随机 container ID 与 ownership label 避免共享固定名称。

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

如果端口 5000 已占用,应停止并查明实际 owner;不能删除已有 Container 或 Listener。也可以使用操作者提供的 Registry,认证方式见 [Demo](../test/demo/DEMO_zh.md)。

## 5. 准备、构建、创建、访问、暂停、恢复与销毁

跨 `sudo` 的值全部显式传入。`DEMO_DATA_DIR` 由 root 所有且保持私有;`PYTHON_BIN` 始终是用户创建的 virtual environment 中的绝对解释器路径。

```bash
sudo -n env DEMO_DATA_DIR="$DEMO_DATA_DIR" BIN="$BIN" \
    REGISTRY=127.0.0.1:5000 REGISTRY_INSECURE=1 \
    bash "$INSTALL_DIR/test/demo/demo_prep.sh"

sudo -n env DEMO_DATA_DIR="$DEMO_DATA_DIR" BIN="$BIN" \
    PYTHON_BIN="$PYTHON_BIN" DEMO_QUICKSTART=1 \
    bash "$INSTALL_DIR/test/demo/demo_e2b.sh"
```

首次准备会拉取按 digest 固定的精简 Docker Official `python:3.12-slim` linux/amd64 manifest,再使用 digest 派生的目标 Tag 推入 Registry。脚本会回读目标内容;如同名内容不同则拒绝覆盖。

只有完成以下全部操作,Quick Start 才成功:构建并回读 `snp` Template target、创建真实 MicroVM、执行 Guest 命令、通过 Files API 写入/读取、验证直连和认证数据访问及 outbound NAT、确认两类数据跨 pause/resume 保留、调用 `kill()`,并完成运行所属清理。

## 6. 清理

先停止持久 Demo 服务。`stop` 保留 cache 数据;`reset` 停止服务后只删除带精确 marker 的 Demo 目录。

```bash
sudo -n env DEMO_DATA_DIR="$DEMO_DATA_DIR" \
    bash "$INSTALL_DIR/test/demo/demo_prep.sh" stop
# 或者在检查任何清理不完整报告后执行:
sudo -n env DEMO_DATA_DIR="$DEMO_DATA_DIR" \
    bash "$INSTALL_DIR/test/demo/demo_prep.sh" reset

test "$(sudo -n docker inspect --format '{{ index .Config.Labels "io.kuasar-sandbox.demo-run" }}' "$REGISTRY_CID")" = "$RUN_ID"
sudo -n docker rm -f "$REGISTRY_CID"
```

删除 Container 前必须回读 label。Demo 不会用通配符停止全部 Sandbox unit,也不会删除预存 vSwitch 或 namespace。如果报告归属不明或清理不完整,本次运行失败,并保留 root-only 诊断供检查。

## 7. 当前源码模式

开发场景,或 Stable 渠道尚早于修正后 Demo 合同时,把六个公共仓库 clone 为兄弟目录,记录其精确 SHA,构建该 source set,再按 [Demo](../test/demo/DEMO_zh.md) 的源码模式命令运行。不要分别使用各组件 GitHub Latest;兼容的 Release 组合由聚合 Release 选择。

项目根目录不是 monorepo:

```text
<workspace>/
├── kuasar-sandbox/
├── accelerator/
├── connector/
├── guest-runtime/
├── sandboxer/
└── orchestrator/
```

## 8. 故障排查与后续步骤

- `systemd is not PID1`:使用由 systemd 启动的 Linux 主机,不能使用普通 Container。
- `/dev/kvm not available (rw)`:启用虚拟化并让 root 可以访问该设备。
- `net.ipv4.ip_forward must already be 1`:按主机策略配置 forwarding;Demo 有意不修改该设置。
- `selected Release predates ...`:不能为该组二进制从 `main` 获取脚本。应使用其 bundled historical guide,或构建一套精确的当前 source set。
- Registry、listener、unit、vSwitch、namespace、link、host mapping 或 iptables 冲突会保持不动。
- `DEMO_NETDIAG=1` 只用于诊断,不能把失败的网络断言变成验收成功。

完整 Demo 还包括 `COPY`、暂停态 Template 扇出和迁移。另见 [Demo](../test/demo/DEMO_zh.md)、[部署](deployment_zh.md)、[Release](release_zh.md)和[安全策略](../SECURITY_zh.md)。
