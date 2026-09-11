[English](DEMO.md) | [简体中文](DEMO_zh.md)

# E2B 兼容沙箱主机 Demo

本 Demo 通过未修改的 E2B Python SDK 驱动一个 Kuasar Sandbox 单节点。它使用当前独立的 Conductor 和 Proxy 进程,在 Builder MicroVM 内构建 snapshot template,创建真实 MicroVM,执行 Command 与 Files API,验证数据访问,暂停并恢复同一逻辑 Sandbox,最后销毁 Sandbox。完整模式还覆盖 Template 扇出和一步迁移。

Demo 是可执行的产品入口,但不能代替组件与聚合 Integration E2E。没有可读写 KVM 的运行,或通过 `DEMO_NETDIAG` 容忍网络断言失败的运行,都不能作为验收证据。

## 1. 执行模式

一次运行只能使用一套内部一致的 source set:

- **源码模式:** 用项目 `Makefile` 构建六个兄弟仓,再以同一源码 revision 的脚本运行对应 `bin/<arch>` 目录。
- **Release 模式:** 解析一个聚合 Release Tag,校验该 Release 的全部资产,完成解包后只使用该解包目录中的脚本和二进制。不得把 `main` 上的脚本与较旧 Stable Release 的二进制混用。

`demo_prep.sh` 负责 Demo 持久层:Manifest Store、分层 Cache、Registry 配置、不可变基础镜像 seed,以及 `COPY` 使用的可选 VersityGW。`demo_e2b.sh` 负责一次临时运行:TLS、凭据、Conductor、Proxy、systemd unit、vSwitch、network namespace、NAT 规则、host 映射、Sandbox 和私有工作文件。

COPY 存储保持主机可达:Conductor 执行 HEAD/预签名,SDK 直接上传,Builder 在主机
下载上下文后再流式送入构建 VM。Registry 镜像导入不同:它在 guest 中执行,
通过 Demo 的管理 VIP 路由访问本地 Zot。

## 2. 验证内容

| 阶段 | 操作 | 必须得到的结果 |
| --- | --- | --- |
| 准备 | Store/Cache 使用私有 Unix socket;使用本地 Zot 或选定 Registry | 协议健康检查成功,并从目标 Registry 回读 digest 固定的基础镜像 |
| 配置 | `node-ctl config conductor` 与 `node-ctl config proxy` | 当前 Conductor 与独立 Proxy 配置都通过校验 |
| 就绪 | Conductor `/health`、Proxy TLS 响应和 stats socket | 控制面与数据面分别就绪;Conductor 不处理数据面形状的请求 |
| 租户 | `manifest-key add` 与 E2B API key | Registry 凭据在 key 创建时关联,密码不出现在命令行 |
| 构建 | `Template.build(...)` 加非空 start/ready command | 现有 auto target 解析为 memory Sandbox;精确 Build 的 ready 状态返回 kind `snp` 和供 create 使用的已发布 Template ID |
| 创建 | `Sandbox.create(template)` | 真实 Cloud Hypervisor MicroVM 可通过数据 Proxy 使用 |
| 数据 | `commands.run`、`files.write`、`files.read`、暴露端口 | Guest 执行和两类数据访问都返回断言内容 |
| 状态 | `pause` 后执行 `Sandbox.connect(id)` | Command 与 Files API 写入的数据跨 snapshot/resume 保留 |
| 扇出 | `export-sandbox --to-template` 后执行 `Sandbox.create` | 新 child 出现在列表中,Command 写入的数据与 Files API 数据均通过断言 |
| 迁移 | `Sandbox.connect(id, headers={migration-token})` | 一次 SDK 调用完成 import+resume,再次断言两类数据的内容 |
| 销毁 | `Sandbox.kill` 与运行所属清理 | Sandbox 和所有能安全证明归属的临时 host 资源都已消失 |

Quick Start 模式(`DEMO_QUICKSTART=1`)在 build、create、执行/数据访问、pause/resume 和 kill 后结束。完整模式要求 VersityGW,并继续执行扇出与迁移。

SDK 会把 `Template.build(headers=...)` 同时传给注册和触发请求,而 Kuasar Builder
配置只允许在注册时提供。因此 Demo 保持自动目标,通过 start/ready command 选择快照,
再严格检查最终快照结果。固定版本 SDK 等待结束后仍返回注册 handle;Demo 从该精确
Build 的状态中读取已发布、可供 create 使用的 Template ID。

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

默认基础镜像是按 digest 固定的 Docker Official `python:3.12-slim` linux/amd64 manifest。模板构建使用 `set_user("root")` 创建 E2B 默认 `user` 账号 (UID/GID 1000:1000),随后在 COPY 和快照启动前调用 `set_user("user")`;已有同名账号必须使用这些 ID。这些调用生成 Builder USER 步骤,不使用 SDK 可选的 `run_cmd(user=...)` 参数。其 Python Runtime 足以完成就绪、执行和数据访问检查。覆盖镜像在该账号不存在时还必须提供 `useradd`。如需使用已有 Registry 而不是 Demo 所属 Zot,传入 `REGISTRY=<host[:port]>`。需要认证时传入 `REGISTRY_USER` 与 `REGISTRY_PASS`;`demo_prep.sh` 通过 stdin 向 `docker login` 传密码,Docker 认证只存放在私有 Demo 数据目录。只有明确使用 HTTP Registry 时才设置 `REGISTRY_INSECURE=1`。覆盖 `E2E_IMAGE` 时必须使用不可变的 `name@sha256:<digest>` 引用。

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
非空且无 marker 的目录或无效 ownership marker 会在目录内容或权限改变之前被拒绝。
默认交换机名为 `k` 加 run ID 的前八个字符。自定义 `SWITCH` 必须为 1–9 个
安全字符,为 Connector 的 `-dummy` 和端口后缀预留空间,不超过 Linux 接口名
15 字节限制。名称冲突会被拒绝,不会被接管。
新启动的子进程最多有 30 秒完成 setsid/exec 交接;服务就绪检查前会跟踪其实际
可执行文件和启动时间。该启动交接处理不适用于任何预存 PID record。

每次运行会保留指向其私有工作目录的短 socket alias `/run/kd-<run-id>`,使最长的 Sandbox 和 Build socket 路径不超过 Linux 限制。已有 alias 会被视为冲突。清理只删除仍指向本次运行目录的 alias。

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

vSwitch 为每个 Sandbox 从 `100.100.96.0/20` 分配 floating IP,E2B guest profile 则复用 inner 地址 `169.254.0.21/30` 和 next hop `169.254.0.22`。本次运行添加带唯一标记的 forwarding 与 masquerade 规则。本地 Registry 通过 `169.254.169.254` 上的 `--mgmt-service` 暴露给 Builder MicroVM。VersityGW 保持在 host loopback,供主机侧 COPY 路径使用,不经过这个 guest 路由。脚本不会把这两个服务暴露到外部网络。

Demo 同时验证直连 `http://<floating-ip>:8000` 和经过认证的 E2B 数据入口 `https://8000-<sid>.<domain>`,并使用真实 `X-Access-Token`。Guest egress 只是对 Demo 现有 NAT 路径的断言,不是新增产品 Egress 实现。

## 8. 故障排查与 See Also

- Python package 缺失或版本不匹配时,应在独立 virtual environment 中修复;脚本要求精确的 `e2b==2.25.1`。
- Listener、unit、vSwitch、namespace、link、host mapping 或 iptables 冲突不会被自动删除。请使用空闲主机,或在 Demo 之外由实际 owner 处理指明的对象。
- 首次准备可能需要较长时间拉取并推送 digest 固定的基础镜像。目标拉取失败只有在 Registry 明确返回 manifest 不存在时才会被视为“尚未 seed”。
- `cleanup was incomplete` 表示运行失败。重试前检查保留的 root-only 目录和指明的 host 对象。

另见 [Quick Start](../../docs/quickstart_zh.md)、[单节点设计](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node_zh.md)、[Builder 设计](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-build_zh.md)和 [Integration E2E 指南](../QUICKSTART_zh.md)。
