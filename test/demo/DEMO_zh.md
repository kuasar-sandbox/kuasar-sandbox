[English](DEMO.md) | [简体中文](DEMO_zh.md)

# E2B 兼容沙箱主机 Demo

本 Demo 通过未修改的 E2B Python SDK 驱动一个 Kuasar Sandbox 单节点。它使用当前独立的 Conductor 和 Proxy 进程,在 Builder MicroVM 内构建 snapshot template,创建真实 MicroVM,执行 Command 与 Files API,验证数据访问,暂停并恢复同一逻辑 Sandbox,最后销毁 Sandbox。完整模式还覆盖 Template 扇出和一步迁移。

Demo 是可执行的产品入口,但不能代替组件与聚合 Integration E2E。没有可读写 KVM 的运行,或通过 `DEMO_NETDIAG` 容忍网络断言失败的运行,都不能作为验收证据。

## 1. 执行模式

一次运行只能使用一套内部一致的 source set:

- **源码模式:** 用项目 `Makefile` 构建六个兄弟仓,再以同一源码 revision 的脚本运行对应 `bin/<arch>` 目录。
- **Release 模式:** 解析一个聚合 Release Tag,校验该 Release 的全部资产,完成解包后只使用该解包目录中的脚本和二进制。不得把 `main` 上的脚本与较旧 Stable Release 的二进制混用。

`demo_prep.sh` 负责 Demo 持久层:Manifest Store、分层 Cache、Registry 配置、不可变基础镜像 seed,以及 `COPY` 使用的可选 VersityGW。`demo_e2b.sh` 负责一次临时运行:TLS、凭据、Conductor、Proxy、systemd unit、vSwitch、network namespace、NAT 规则、host 映射、Sandbox 和私有工作文件。

## 2. 验证内容

| 阶段 | 操作 | 必须得到的结果 |
| --- | --- | --- |
| 准备 | Store/Cache 使用私有 Unix socket;使用本地 Zot 或选定 Registry | 协议健康检查成功,并从目标 Registry 回读 digest 固定的基础镜像 |
| 配置 | `node-ctl config conductor` 与 `node-ctl config proxy` | 当前 Conductor 与独立 Proxy 配置都通过校验 |
| 就绪 | Conductor `/health`、Proxy TLS 响应和 stats socket | 控制面与数据面分别就绪;Conductor 不处理数据面形状的请求 |
| 租户 | `manifest-key add` 与 E2B API key | Registry 凭据在 key 创建时关联,密码不出现在命令行 |
| 构建 | `Template.build(..., headers={"X-Kuasar-Sandbox-Builder": ...})` | 回读到请求的 sandbox-with-memory 目标,产物 Template kind 为 `snp` |
| 创建 | `Sandbox.create(template)` | 真实 Cloud Hypervisor MicroVM 可通过数据 Proxy 使用 |
| 数据 | `commands.run`、`files.write`、`files.read`、暴露端口 | Guest 执行和两类数据访问都返回断言内容 |
| 状态 | `pause` 后执行 `Sandbox.connect(id)` | Command 与 Files API 写入的数据跨 snapshot/resume 保留 |
| 扇出 | `export-sandbox --to-template` 后执行 `Sandbox.create` | 新 child 携带导出的状态 |
| 迁移 | `Sandbox.connect(id, headers={migration-token})` | 一次 SDK 调用完成 import+resume,状态不变 |
| 销毁 | `Sandbox.kill` 与运行所属清理 | Sandbox 和所有能安全证明归属的临时 host 资源都已消失 |

Quick Start 模式(`DEMO_QUICKSTART=1`)在 build、create、执行/数据访问、pause/resume 和 kill 后结束。完整模式要求 VersityGW,并继续执行扇出与迁移。

## 3. 主机与工具前置

预构建资产支持 Linux x86_64 和 glibc 2.38 或更高版本。真实运行还需要:

- systemd 为 PID 1、cgroup v2、root 和可读写的 `/dev/kvm`;
- 操作者在运行前启用 `net.ipv4.ip_forward=1`;Demo 会拒绝修改这个 host-global 设置;
- `127.0.0.1:443` 与 `127.0.0.2:443` 空闲,没有标准 `sandbox-runner@*`/`sandbox-builder@*` 实例,也没有冲突的 Demo vSwitch、namespace、link、unit、host entry 或 iptables marker;
- `openssl`、`ip`、`curl`、`sqlite3`、`iptables`、`flock`、`setsid`、`timeout`、`mkfs.ext4`,以及用于向 OCI Registry seed 镜像的 Docker;
- 在独立 virtual environment 中安装 [`requirements.txt`](requirements.txt) 固定的 Python 依赖;
- 同一 source set 中的 `node-ctl`、`e2b-key-ctl`、`connector-ctl`、`store-ctl`、`cache-ctl`、`cloud-hypervisor`、`vmlinux` 与 `sandbox-runtime.bundle`。

完整模式还需要 `versitygw`。源码模式通过 `make e2e-tools` 取得固定版本的 Zot 与 VersityGW。Release 模式可以使用操作者提供的 Registry 和 VersityGW;较短 Release-first 流程见 [Quick Start](../../docs/quickstart_zh.md)。

Builder Sandbox 请求 2 vCPU 和 6 GiB capacity。还应为 host 和从 Template 创建的 Sandbox 保留 CPU 与内存。

## 4. 源码模式运行

在包含六个兄弟仓的父目录执行:

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

跨越 `sudo` 的路径全部显式使用绝对路径。脚本不依赖调用者与 root 恰好使用相同 `HOME` 或 Python 安装。

如需使用已有 Registry 而不是 Demo 所属 Zot,传入 `REGISTRY=<host[:port]>`。需要认证时传入 `REGISTRY_USER` 与 `REGISTRY_PASS`;`demo_prep.sh` 通过 stdin 向 `docker login` 传密码,Docker 认证只存放在私有 Demo 数据目录。只有明确使用 HTTP Registry 时才设置 `REGISTRY_INSECURE=1`。覆盖 `E2E_IMAGE` 时必须使用不可变的 `name@sha256:<digest>` 引用。

## 5. 控制项与重复运行

```bash
# 只运行较短生命周期。
sudo -n env DEMO_DATA_DIR="$DEMO_DATA_DIR" BIN="$BIN" \
    PYTHON_BIN="$PYTHON_BIN" DEMO_QUICKSTART=1 \
    bash "$PWD/kuasar-sandbox/test/demo/demo_e2b.sh"

# 在阶段间暂停;脚本会打印供另一个 root 终端使用的私有 cli.env。
sudo -n env DEMO_DATA_DIR="$DEMO_DATA_DIR" BIN="$BIN" \
    PYTHON_BIN="$PYTHON_BIN" DEMO_PAUSE=1 \
    bash "$PWD/kuasar-sandbox/test/demo/demo_e2b.sh"

# 只保留未检测到运行密钥的日志。
sudo -n env DEMO_DATA_DIR="$DEMO_DATA_DIR" BIN="$BIN" \
    PYTHON_BIN="$PYTHON_BIN" DEMO_KEEP=1 \
    bash "$PWD/kuasar-sandbox/test/demo/demo_e2b.sh"
```

只有当存活服务仍匹配记录的 executable、start time 与配置时,准备步骤才幂等。每次运行默认获得新的随机身份。可以设置 `DEMO_RUN_ID` 以便复现,但已有 work/result 目录或 host marker 会被视为冲突,不会被接管。

`DEMO_NETDIAG=1` 会在直连端口或 egress 断言失败后继续诊断。此模式不能作为 Demo 或 Release 验证成功的证据。

## 6. 归属、凭据与清理

`DEMO_DATA_DIR` 默认是 `/var/lib/kuasar-demo`。它必须是只含安全路径字符的 canonical absolute path。脚本创建 root 所属、mode 0700 的目录和严格 ownership marker。非空且无 marker 的目录、symlink 路径、异常 PID record、存活服务配置变化或陌生 host 资源都会触发 fail-closed 拒绝。

`prep.env`、Docker 认证、服务配置、生成的 key、TLS private key 和可选 `cli.env` 均位于运行所属目录并使用私有权限。准备交接使用 shell assignment 而不是导出 secret;长期运行的 Conductor、Proxy、Store 与 Cache 不会无必要地继承 Registry 或对象存储密码。复制进新 node business record 的凭据保持与该记录关联;之后修改 key-distribution entry 不会重绑已有记录。

正常退出和普通失败时,`demo_e2b.sh` 只停止它启动的精确 unit instance 与 process group,只删除带本次标记的 iptables 和 `/etc/hosts` 条目,并且仅在能证明归属后删除 vSwitch 或 namespace。脚本不会用通配符停止全部 Sandbox Runner/Builder。如果归属变得不明确或清理不完整,脚本会保留私有工作目录并报告失败,不会强制删除该对象。

持久准备层可以供下次运行复用:

```bash
sudo -n env DEMO_DATA_DIR="$DEMO_DATA_DIR" \
    bash "$PWD/kuasar-sandbox/test/demo/demo_prep.sh" stop
sudo -n env DEMO_DATA_DIR="$DEMO_DATA_DIR" \
    bash "$PWD/kuasar-sandbox/test/demo/demo_prep.sh" reset
```

`stop` 只停止由有效运行所属 PID record 标识的服务并保留数据。`reset` 先停止这些服务,再只删除带精确 marker 且明确采用 Demo 名称的数据目录。应先解决报告的归属不明外部资源,再删除其诊断目录。

## 7. 网络与入口布局

Conductor 监听 `127.0.0.1:443`,独立 Proxy 监听 `127.0.0.2:443`。本地 Demo CA 为 `*.<domain>` 签发证书,SDK 调用使用该 CA 文件和 `NO_PROXY=*`。脚本在取得每个 Sandbox ID 后逐项添加 `/etc/hosts` 记录,并按本次 marker 删除;不假定存在 wildcard DNS。

vSwitch 为每个 Sandbox 从 `100.100.96.0/20` 分配 floating IP,E2B guest profile 则复用 inner 地址 `169.254.0.21/30` 和 next hop `169.254.0.22`。本次运行添加带唯一标记的 forwarding 与 masquerade 规则。绑定在 host loopback 的本地 Registry 和 VersityGW 通过 `169.254.169.254` 上的 `--mgmt-service` 暴露给 Builder MicroVM;脚本不会把这些服务暴露到外部网络。

Demo 同时验证直连 `http://<floating-ip>:8000` 和经过认证的 E2B 数据入口 `https://8000-<sid>.<domain>`,并使用真实 `X-Access-Token`。Guest egress 只是对 Demo 现有 NAT 路径的断言,不是新增产品 Egress 实现。

## 8. 故障排查与 See Also

- Python package 缺失或版本不匹配时,应在独立 virtual environment 中修复;脚本要求精确的 `e2b==2.25.1`。
- Listener、unit、vSwitch、namespace、link、host mapping 或 iptables 冲突不会被自动删除。请使用空闲主机,或在 Demo 之外由实际 owner 处理指明的对象。
- 首次准备可能需要较长时间拉取并推送 digest 固定的基础镜像。目标拉取失败只有在 Registry 明确返回 manifest 不存在时才会被视为“尚未 seed”。
- `cleanup was incomplete` 表示运行失败。重试前检查保留的 root-only 目录和指明的 host 对象。

另见 [Quick Start](../../docs/quickstart_zh.md)、[单节点设计](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node_zh.md)、[Builder 设计](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-build_zh.md)和 [Integration E2E 指南](../QUICKSTART_zh.md)。
