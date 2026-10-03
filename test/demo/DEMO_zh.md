[English](DEMO.md) | [简体中文](DEMO_zh.md)

# Workbench 中的 E2B Demo

本 Demo 通过未修改的 E2B Python SDK 驱动真实 Kuasar 单节点。Workbench 提供系统
环境，所选聚合发布版提供产品、脚本、原生 helper 和锁定 SDK wheel。Workbench
不是生产部署的强制依赖，也不是针对恶意 root 的安全边界。

## 1. 发布版与 workbench 流程（推荐）

完成[快速开始](../../docs/quickstart_zh.md)直至离线准备，保留宿主变量 `WB`、`STATE`、
`NAME`、`IMAGE`、`RELEASE` 和 `ARCH`。所选版本须包含本文的 `prepared.py` 适配器；
不要从 `main` 将它复制到旧发布版，应使用旧版本自身指南。

宿主调用 `workbench exec`，`exec --` 之后的内容在同一个系统实例内执行。
`/inputs/release` 只读；`/work/prepared` 是封存输入，不用于安装依赖或存放可变
Demo 状态。执行完整 Demo：

```bash
python3 "$WB" --root "$STATE" --name "$NAME" exec -- \
  python3 -B /work/prepared/test/demo/prepared.py run \
  --workdir /work/prepared --data-dir /work/kuasar-demo-first
```

添加 `--quick` 只执行首次体验生命周期；添加 `--pause` 在阶段间暂停观察：

```bash
python3 "$WB" --root "$STATE" --name "$NAME" exec -- \
  python3 -B /work/prepared/test/demo/prepared.py run \
  --workdir /work/prepared --data-dir /work/kuasar-demo-first --pause
```

在第二个**宿主终端**恢复相同的 `WB`、`STATE` 和 `NAME`，进入同一个实例：

```bash
python3 "$WB" --root "$STATE" --name "$NAME" exec
```

在该 shell 内使用 Demo 打印的私有 `cli.env` 路径。TLS CA、DNS 条目、控制面
`127.0.0.1:443`、数据面 `127.0.0.2:443` 及 floating IP 均属于 workbench 的网络视图，
不属于外层宿主。本演示不需要公开宿主端口或挂载宿主 Docker socket。

适配器验证预备内容/权限、原生镜像和 helper，在输入之外创建隔离 SDK 启动器，再
调用现有脚本。它不实现第二套 E2E runner，也不会在执行时安装 Python 包。
这些脚本仍然是 `basic.demo.sh` 使用的共同实现。

保留 bridge 网络：offline prepare 禁止获取依赖，但短/完整 Demo 都保留真实
Internet 出站检查。Workbench 成功启动或诊断运行不等于 Demo 成功。完整产品用例
还验证外来资源拒绝、重复准备及清理；通过[公共 E2E runner](../QUICKSTART_zh.md)
执行这些额外断言。

## 2. 演示内容

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
| 扇出 | `export-sandbox --to-template` 后执行 `Sandbox.create` | 先通过 child 的 guest Command 与 Files API 数据断言,再要求它出现在公开列表中;`starting` 记录会刻意隐藏 |
| 迁移 | `Sandbox.connect(id, headers={migration-token})` | 一次 SDK 调用完成 import+resume,再次断言两类数据的内容 |
| 销毁 | `Sandbox.kill` 与运行所属清理 | Sandbox 和所有能安全证明归属的临时 host 资源都已消失 |

Quick Start 模式(`DEMO_QUICKSTART=1`)在 build、create、执行/数据访问、pause/resume 和 kill 后结束。完整模式要求 VersityGW,并继续执行扇出与迁移。

SDK 会把 `Template.build(headers=...)` 同时传给注册和触发请求,而 Kuasar Builder
配置只允许在注册时提供。因此 Demo 保持自动目标,通过 start/ready command 选择快照,
再严格检查最终快照结果。固定版本 SDK 等待结束后仍返回注册 handle;Demo 从该精确
Build 的状态中读取已发布、可供 create 使用的 Template ID。


## 3. 重复运行、日志与清理

`demo_prep.sh` 管理持久 Store/Cache/Registry 及可选 COPY Gateway；`demo_e2b.sh`
管理每次运行的 Conductor/Proxy、unit、网络、凭据和沙箱。示例中的预备适配器使用
`/work/kuasar-demo-first`，只传递任务输入，并设置 `DEMO_KEEP=1` 保留安全日志。
它不继承短流程、网络诊断模式或外部 Docker 端点。

重复运行同一命令会复用准备层，并产生新的运行身份。查看数据根目录下的准备
`logs/`、安全保留的 `results/` 及错误报告的私有目录；宿主映射为
`$STATE/$NAME/work/kuasar-demo-first`。通过 workbench 或已授权宿主权限查看
root-only 文件，不要公开私有配置。停止准备层但保留数据和日志：

```bash
python3 "$WB" --root "$STATE" --name "$NAME" exec -- \
  python3 -B /work/prepared/test/demo/prepared.py stop \
  --workdir /work/prepared --data-dir /work/kuasar-demo-first
```

仅在保存必要诊断后，重置精确归属的 Demo 数据：

```bash
python3 "$WB" --root "$STATE" --name "$NAME" exec -- \
  python3 -B /work/prepared/test/demo/prepared.py reset \
  --workdir /work/prepared --data-dir /work/kuasar-demo-first
```

`reset` 同时删除该 Demo 的日志和状态，不是默认故障处理方式。Workbench 的 `stop`
和 `cleanup` 是独立外层生命周期操作；普通 `cleanup` 保留 `/work` 和 `/output`，
`--delete-output` 才显式删除。详见 [Workbench](../../workbench/README_zh.md)。已清理
实例需要新名称，停止但未清理的实例只能按原设置重启。

## 4. 原生源码替代路径

使用记录完整的六仓源码集合及对应构建产物。这条路径直接运行于原生系统环境，
不在普通 UID 的 workbench build 模式中执行。需要 systemd PID 1、cgroup v2、
root/非交互 sudo、KVM、产品原生依赖库、Python 3.12 和普通 Demo 工具（openssl、
ip、curl、sqlite3、iptables、flock、setsid、timeout、mkfs.ext4、Docker）。执行前按
原生宿主策略启用 forwarding，Demo 不修改该设置；两个 loopback TLS 监听地址
及 Demo 资源须空闲。原生生产拓扑见[部署](../../docs/deployment_zh.md)。

在六个兄弟仓的父目录执行：

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

`make demo PYTHON_BIN=/absolute/path/to/python` 是**源码专用**便捷入口，先检查
解释器，再构建产品和工具，不是发行版安装器。Workbench 普通 UID 的 `--mode build`
提供编译器，不提供 `Template.build()` 或 MicroVM 所需权限/设备。源码构建说明见
[Workbench](../../workbench/README_zh.md)。

独立原生脚本保留 `DEMO_QUICKSTART`、`DEMO_PAUSE`、`DEMO_KEEP`、`DEMO_NETDIAG`
控制项，通过 `sudo -n env` 显式传递。公共完整产品用例不使用短流程或网络诊断模式。
完整 Demo 需要 VersityGW 完成 COPY；所有跨 sudo 的路径均须为绝对路径。

默认逻辑基础镜像为 `python:3.12-slim`，原生平台在 x86_64 上为 `linux/amd64`，在
 aarch64 上为 `linux/arm64`。独立准备可拉取缺失的名称/tag/digest；制品执行只消费
精确的预备本地镜像 ID，不选择或拉取替代镜像。错误架构镜像会被拒绝。准备阶段发布
同一 config 身份，再回读目标 manifest digest；源 image ID 与 registry digest
不是同一种身份。已有 Registry 凭据和 `REGISTRY_INSECURE=1` 仍是原生脚本的显式
选项；发布版适配器使用预备的本地 Zot。

模板通过 Builder USER 步骤在 COPY/启动前创建默认 `user`（UID/GID 1000:1000）。
替换源码镜像时，若该账号不存在，须提供 `useradd`。新稀疏 overlay/builder 盘采用
`mkfs.ext4 -O ^has_journal`，去掉的是文件系统 journal，不是应用或 journald 日志；
Guest sync 与快照恢复语义保持不变。

## 5. 归属与凭据

以下原生脚本合同同样适用于 **workbench 内部**；此处“主机”指 Demo 执行环境，
不授权操作外层 Docker 宿主。适配器使用上面的显式 `/work` 目录，独立脚本仍保留
其原生默认目录。

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


## 6. 网络布局

Conductor 监听 `127.0.0.1:443`,独立 Proxy 监听 `127.0.0.2:443`。本地 Demo CA 为 `*.<domain>` 签发证书,SDK 调用使用该 CA 文件和 `NO_PROXY=*`。脚本在取得每个 Sandbox ID 后逐项添加 `/etc/hosts` 记录,并按本次 marker 删除;不假定存在 wildcard DNS。

vSwitch 为每个 Sandbox 从 `100.100.96.0/20` 分配 floating IP,E2B guest profile 则复用 inner 地址 `169.254.0.21/30` 和 next hop `169.254.0.22`。本次运行添加带唯一标记的 forwarding 与 masquerade 规则。本地 Registry 通过 `169.254.169.254` 上的 `--mgmt-service` 暴露给 Builder MicroVM。VersityGW 保持在 host loopback,供主机侧 COPY 路径使用,不经过这个 guest 路由。脚本不会把这两个服务暴露到外部网络。

Demo 同时验证直连 `http://<floating-ip>:8000` 和经过认证的 E2B 数据入口 `https://8000-<sid>.<domain>`,并使用真实 `X-Access-Token`。Guest egress 只是对 Demo 现有 NAT 路径的断言,不是新增产品 Egress 实现。


## 7. 故障排查

发布版模式缺少/不匹配 SDK 或 helper 时，应取得匹配输入并重新 prepare，而不是
回退到全局 pip/PATH。原生源码模式自行管理显式虚拟环境。缺少 KVM/内核功能时，
必须在所选原生环境解决；模拟或跳过断言不算成功。资源冲突时应检查实例内 owner，
不要停止无关服务。清理失败时保留报告中的私有诊断，先处理归属再 reset。

另见[快速开始](../../docs/quickstart_zh.md)、[发布验证](../QUICKSTART_zh.md)、
[Node](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node_zh.md) 和
[Builder](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-build_zh.md)。
