# Quick Start

本文面向第一次使用 Kuasar Sandbox 的用户,从一个聚合 Release 启动单节点,
并用未修改的 E2B Python SDK 构建模板,创建真实 MicroVM,执行命令,暂停,恢复和
销毁沙箱.

这是最短使用路径,不是完整发布验收.网络,TLS,Registry 和内部数据路径的详细
说明见 [Demo](../test/demo/DEMO.md);六个 owner 套件和平台组合门禁见
[Aggregate Release Validation Guide](../test/QUICKSTART.md).

## 1. 检查环境

当前公开聚合 Release 的预构建资产支持 Linux x86_64,并需要 glibc 2.38 或更高
(例如 glibc 2.39 的 Ubuntu 24.04).主机还需要 systemd 作为 PID 1,cgroup v2,可读写的
`/dev/kvm`,root 或无交互 `sudo`,以及 Python 3.下载阶段使用 GitHub CLI 和可用的
GitHub 登录态;MicroVM 运行本身不依赖它.示例的构建沙箱请求 2 vCPU 和 6 GiB 内存,
节点还需要为系统服务和后续运行沙箱保留余量.

Ubuntu 24.04 可以用以下命令安装本 Quick Start 需要的 host 工具和动态库:

```bash
sudo apt-get update
sudo apt-get install -y \
    ca-certificates curl docker.io e2fsprogs gh iproute2 iptables \
    libgcc-s1 liblz4-1 libsnappy1v5 libstdc++6 libzstd1 openssl \
    procps python3 python3-venv sqlite3 tar util-linux zlib1g
```

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
glibc_version="$(getconf GNU_LIBC_VERSION | awk '{print $2}')"
test "$(printf '%s\n' 2.38 "$glibc_version" | sort -V | head -n 1)" = 2.38
for tool in gh tar sha256sum python3 openssl ip curl sqlite3 iptables mkfs.ext4 docker ldd; do
    command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done
gh auth status
sudo -n docker info >/dev/null
)
```

上述代码块只在所有检查通过时返回成功;任一命令失败时不要继续.

Docker 只用于为本 Quick Start 准备本地 OCI Registry 并导入基础镜像,不是所有 Kuasar
Sandbox 部署的运行时依赖.如果已有从构建 MicroVM 可访问的 OCI Registry,可以按
[Demo 的 Registry 配置](../test/demo/DEMO.md)复用它.Zot 只在选择由
`demo_prep.sh` 启动本地 Zot 时需要;versitygw 只在演示 `COPY` 构建步骤时需要.

从源码构建时,Makefile 还支持 `TARGET_ARCH=aarch64`,但这不表示 aarch64 已作为当前
GitHub Release 的预构建资产发布.

## 2. 下载,校验和解压聚合 Release

当前 Stable 聚合版本是
[`release-v0.1.1`](https://github.com/kuasar-sandbox/kuasar-sandbox/releases/tag/release-v0.1.1).
本指南固定使用该聚合 Release 中的 platform 包和六个组件发布单元;不得将旧 platform
脚本与新组件资产混用.

```bash
RELEASE_VERSION=release-v0.1.1
DOWNLOAD_DIR="$PWD/kuasar-download-$RELEASE_VERSION"
INSTALL_DIR="$PWD/kuasar-$RELEASE_VERSION"

test ! -e "$DOWNLOAD_DIR"
test ! -e "$INSTALL_DIR"
mkdir "$DOWNLOAD_DIR" "$INSTALL_DIR"
gh release view "$RELEASE_VERSION" \
    --repo kuasar-sandbox/kuasar-sandbox \
    --json tagName,isPrerelease
gh release download "$RELEASE_VERSION" \
    --repo kuasar-sandbox/kuasar-sandbox \
    --dir "$DOWNLOAD_DIR"
```

`gh release download` 在空目录中下载该聚合 Release 的全部显式资产:一个 platform
archive,六个组件发布单元 archive 和一个 `SHA256SUMS`.组件 archive 的内部版本可以
不同,但它们必须全部来自同一个聚合 Release;不要混用不同日期或聚合版本的资产.

先校验 SHA256,再将七个 archive 解压到同一目录:

```bash
(
    set -e
    cd "$DOWNLOAD_DIR"
    test "$(find . -maxdepth 1 -type f -name '*.tar.gz' | wc -l)" -eq 7
    test -f SHA256SUMS
    sha256sum --quiet -c SHA256SUMS
    for archive in ./*.tar.gz; do
        tar -xzf "$archive" -C "$INSTALL_DIR"
    done
    cd "$INSTALL_DIR"
    test -d bin && test -d deploy && test -d docs && test -d test
    grep -q 'DEMO_QUICKSTART' test/demo/demo_e2b.sh
    for executable in bin/cache-ctl bin/cloud-hypervisor; do
        ldd_output="$(ldd "$executable" 2>&1)" || { printf '%s\n' "$ldd_output" >&2; exit 1; }
        if printf '%s\n' "$ldd_output" | grep -q 'not found'; then
            printf '%s\n' "$ldd_output" >&2
            exit 1
        fi
    done
)
```

`sha256sum` 成功时不输出内容.解压后,`bin/` 和 `deploy/` 来自组件包,`docs/` 和
`test/` 来自 platform 包.

## 3. 安装未修改的 E2B SDK

```bash
cd "$INSTALL_DIR"
python3 -m venv .venv
. .venv/bin/activate
python -m pip install e2b e2b-code-interpreter
python -c 'import e2b; print(e2b.__file__)'
```

本项目使用上游 Python 包,SDK 不需要 fork 或本地补丁.

## 4. 准备单节点服务

以下命令起一个只监听 host loopback 的临时 Registry,然后复用 Release 中的
`demo_prep.sh` 启动持久的 store/cache 服务并导入基础镜像:

```bash
sudo -n docker run -d --name kuasar-quickstart-registry \
    --network host \
    -e REGISTRY_HTTP_ADDR=127.0.0.1:5000 \
    registry:2
export DEMO_DATA_DIR="$PWD/.kuasar-quickstart"
export REGISTRY=127.0.0.1:5000
export REGISTRY_INSECURE=1
sudo -n env \
    PATH="$VIRTUAL_ENV/bin:$PATH" \
    DEMO_DATA_DIR="$DEMO_DATA_DIR" \
    REGISTRY="$REGISTRY" \
    REGISTRY_INSECURE="$REGISTRY_INSECURE" \
    bash test/demo/demo_prep.sh
```

首次执行需要拉取并导入基础镜像,耗时取决于网络和存储.成功结尾为:

```text
prep ready
```

## 5. 构建模板并运行 MicroVM

```bash
sudo -n env \
    PATH="$VIRTUAL_ENV/bin:$PATH" \
    DEMO_DATA_DIR="$DEMO_DATA_DIR" \
    DEMO_QUICKSTART=1 \
    bash test/demo/demo_e2b.sh
```

该脚本准备单节点 TLS,凭据,网络和 E2B 兼容入口,然后用未修改的 SDK 执行
如下核心调用:

```python
from e2b import Sandbox, Template

template = Template().from_image("<registry>/<image>:<tag>")
build = Template.build(template, name="quickstart", cpu_count=2, memory_mb=6144)
sandbox = Sandbox.create(build.template_id, timeout=300)
sandbox_id = sandbox.sandbox_id
print(sandbox.commands.run("uname -sm").stdout)
sandbox.pause()
sandbox = Sandbox.connect(sandbox_id)  # connect auto-resumes a paused sandbox
print(sandbox.commands.run("echo resumed").stdout)
sandbox.kill()
```

SDK 没有独立的 `resume()` 方法;`Sandbox.connect(sandbox_id)` 会重新连接并自动恢复已暂停
的同一逻辑实例.Quick Start 模式还验证 Guest 内执行,状态跨 pause/resume 保留和网络,
并最终调用 `kill()`.完整 Demo 另行覆盖模板扇出和迁移.成功结尾为:

```text
quick start complete
```

## 6. 清理

`demo_e2b.sh` 正常或失败退出时都会清理本次的 systemd unit,vSwitch,NAT 规则,
`/etc/hosts` 临时项和沙箱.停止 store/cache 服务并保留其数据;临时 Registry 没有挂载
持久卷,删除容器会丢弃其中的数据:

```bash
sudo -n env DEMO_DATA_DIR="$DEMO_DATA_DIR" bash test/demo/demo_prep.sh stop
sudo -n docker rm -f kuasar-quickstart-registry
```

停止服务并删除本 Quick Start 的 store,cache 和 Registry 索引数据:

```bash
sudo -n env DEMO_DATA_DIR="$DEMO_DATA_DIR" bash test/demo/demo_prep.sh reset
sudo -n docker rm -f kuasar-quickstart-registry
```

## 7. 常见问题

- `systemd is not PID1`:请在使用 systemd 的 Linux 主机上运行,而不是普通容器内.
- `/dev/kvm not available (rw)`:启用主机虚拟化,加载 KVM,并确认 root 可读写设备.
- `GLIBC_... not found`:当前预构建资产需要 glibc 2.38 或更高;请改用满足条件的
  Linux 主机,不要手工替换系统 libc.
- `missing ... in bin`:确认已下载同一聚合 Release 的全部显式资产,并将七个
  archive 解压到同一目录.
- `e2b Python SDK not installed`:确认 `PATH` 中的 `python3` 来自前述 virtual environment,
  并将同一 `PATH` 传给 `sudo env`.
- Registry 或镜像拉取失败:确认 Docker 能够拉取基础镜像,Registry 端口未被占用;
  使用外部 Registry 时按 [Demo](../test/demo/DEMO.md) 传入访问凭据.
- TLS,网络或端口冲突:检查 TCP 443,Registry 端口,iptables 和本机路由;
  详细数据路径见 [Demo](../test/demo/DEMO.md).

## 8. 支持范围和生产部署

- 当前 Stable 聚合版本是 `release-v0.1.1`,它是非 prerelease 的公开发行版本.
  Preview 仍提供可验证的精确组件组合,但作为 GitHub prerelease 用于开发和评估.
- 单节点模式可直接使用;集群模式需要 registry,router,placer,可达的节点服务,
  共享或远程持久化数据路径和集群网络.
- 镜像和快照可以按部署环境使用本地文件,NAS/NFS 等共享文件路径,或 Manifest,
  S3-compatible 对象存储和分层缓存.
- 基础 vSwitch,沙箱隔离和外部策略网关接入基础已交付.节点本地轻量
  [Egress](https://github.com/kuasar-sandbox/connector/issues/9) 和
  [OpenTelemetry](https://github.com/kuasar-sandbox/kuasar-sandbox/issues/52) 是 Proposed,
  不是本 Quick Start 的前提.
- 生产环境必须使用正式 TLS,可靠存储,网络策略和安全凭据.Demo 生成的本地 CA 和服务器证书,
  本地 Registry 和演示凭据只用于体验.

更完整的单节点和集群拓扑见 [Deployment](deployment.md),组件版本与聚合发布关系见
[Releases](release.md),安全漏洞报告方式见
[Security](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/main/SECURITY.md).
