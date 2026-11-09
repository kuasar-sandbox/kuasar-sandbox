[English](node-infra.md) | [简体中文](node-infra_zh.md)

# 部署与运维 node-infra 系统容器

本指南使用 **node-infra** 镜像和对应部署包交付一个 kuasar-sandbox 数据面节点，
负责宿主与容器绑定、安装及实例生命周期。组件原生配置和 API 的完整契约仍由组件仓库
维护。平台节点池路由、宿主设备分配和外部 Cache/Store 服务部署各有其责任方。

## 前置条件与发布输入

使用原生 amd64 或 arm64 Linux 宿主，提供 systemd、cgroup v2、可用的 KVM/TUN、
网络空间、BPF/TC 与 userfaultfd。安装 rootful Podman、Quadlet generator 和受支持的
OCI runtime。部署包 `release.json` 记录所选支持基线；随包平台 E2E 验证实际模板。
systemd 启动或 generator 检查成功，不能单独作为 VM 与恢复验收通过的证据。

获取同一聚合版本的以下输入：

| 输入 | 用途 |
|---|---|
| `ghcr.io/kuasar-sandbox/node-infra:<version>` | 多架构 OCI 镜像，选择并记录具体平台 manifest。 |
| `node-infra-<version>-linux-<amd64|arm64>.oci.tar.gz` | 对应平台镜像的离线归档，通过 `podman load` 导入。 |
| `node-infra-deploy-<version>.tar.gz` | 宿主工具、完整 Quadlet 模板、运行钩子、示例和本指南。 |
| 对应 workbench 和 `platform-<version>.tar.gz` | 可重复验收环境与对应测试。 |

沿现有 GitHub Release asset size/digest 字段校验下载。`release.json` 记录镜像
index/平台 manifest、镜像配置 ID 与离线归档的对应关系。归档校验值、镜像配置 ID
和 manifest digest 分别标识不同对象。镜像的组件身份也记录在
`/usr/share/node-infra/image.json`。

启动服务前获取或导入镜像，核对平台与镜像身份，保留实例模板中的 `Pull=never`。
例如：

```sh
sudo podman load --input node-infra-VERSION-linux-amd64.oci.tar.gz
# 或从仓库获取所选平台 manifest：
sudo podman pull ghcr.io/kuasar-sandbox/node-infra@sha256:PLATFORM_MANIFEST_DIGEST
```

镜像包含全部节点二进制及默认引导资产。正常启动链包括 Conductor、Proxy、Connector
及其执行 units；Telemetry 使用原生 systemd 启用方式。Cache/Store 端点由节点级
存储访问层提供，部署工具不启动这些外部服务。

## 准备单个实例的宿主资源

准备一个独立网络空间，例如 `/run/netns/node-infra-node-a`：

| 输入 | 启动前状态 | 用途 |
|---|---|---|
| `lo` | UP | 本地通信。 |
| `net0` | 存在、UP，已配置入口地址和路由 | Conductor 和 Proxy 入口。 |
| `net1` | 存在、DOWN | Connector 接管并移入内部 switch 网络空间，承载 transit。 |
| 设备与挂载 | 所选运行器可访问 | KVM/TUN、cgroup 委托及组件创建的网络资源。 |
| 持久目录 | 本实例独立使用 | 节点数据库、可写磁盘与 checkpoint。 |

宿主可以将已经分配的接口移入网络空间，并按默认约定改名。交付接口使用名称和网络
空间路径，不依赖设备是 VF、veth 还是其他受支持类型。transit 地址和网关通过原生
Connector 配置设置。宿主负责入口地址与路由，并在宿主重启后先恢复主网络空间，
再启动实例。

Podman 通过 `Network=ns:...` 加入网络空间。提供可用 DNS 和匹配的
`/etc/resolv.conf`；加入网络空间不会创建该文件。保证实例可访问外部存储端点和所选
文件型制品。UDS 挂载提供方持有的父目录，使 socket 重建后仍可见。存储端点不会让
任意宿主文件自动在容器内可见。

## 使用原生路径配置

Quadlet 管理镜像、网络空间、设备、挂载、实例资源额度及宿主服务依赖。组件原生文件
管理组件配置，不另设 `node.yaml` schema。

| 服务 | 二进制命令 | 主配置 |
|---|---|---|
| `node-conductor.service` | `node-ctl conductor serve` | `/etc/node-conductor/conductor.yaml` |
| `node-proxy.service` | `node-ctl proxy serve` | `/etc/node-proxy/proxy.yaml` |
| `node-telemetry.service` | `node-ctl telemetry serve` | `/etc/node-telemetry/telemetry.yaml` |
| `connector-vswitch.service` | `connector-ctl vswitch serve` | `/etc/connector-vswitch/vswitch.yaml` |

每项服务先读取镜像主文件，再读取按名称排序的 `/etc/<component>/conf.d/*.yaml`。
启动脚本向原生加载器传入一个明确的 `--config PATH[:PATH...]` 参数值。对象递归
合并；后层标量和列表替换前层值，覆盖层 null 删除继承字段。文件发现仅在启动脚本中
执行。交付的全部 Connector 文件使用 YAML。Manifest 保留原生单文件入口
`/etc/node-infra/manifest.yaml`。

站点策略放在 `50-platform.yaml`，实例绑定放在 `90-instance.yaml`。挂载单文件或
`conf.d` 目录，保留镜像主配置。客户 Containerfile COPY 使用相同路径。保留
`/opt/node-infra/bin/` 下的相邻 helper 关系，以及 `/opt/node-infra/boot/` 下默认的
`vmlinux` 和 `sandbox-runtime.bundle`。

按部署提供原生域名与监听、鉴权/TLS、加密密钥和 Manifest 端点。默认内部 API/数据
端口为 3000/3001。私有配置文件由 root 持有并限制访问。以下为实例覆盖片段，不替换
主配置：

```yaml
# /etc/node-conductor/conf.d/90-instance.yaml
api:
  domain: sandboxes.example.com
  listen: 0.0.0.0:3000
# 添加本部署的原生 encryption_key 和 API TLS/鉴权设置。
sandbox:
  boot:
    overlay_diff_template: /var/lib/node-infra/overlay.ext4
```

```yaml
# /etc/connector-vswitch/conf.d/90-instance.yaml
transit_dev: net1
transit_dev_addr: "192.0.2.6/30:192.0.2.5"
```

Image 冷启动需要在实例持久数据目录中准备新的稀疏 ext4 overlay 模板，并引用其
容器路径。例如，`DATA` 为绑定到 `/var/lib/node-infra` 的宿主目录时：

```sh
test ! -e "$DATA/overlay.ext4"
truncate -s 4G "$DATA/overlay.ext4"
mkfs.ext4 -F -q -b 4096 -O ^has_journal "$DATA/overlay.ext4"
```

Build 工作负载单独配置原生 `builder.diff_template` 及源/发布依赖。命名文件制品
使用原生 `checkpoint.remote.ref_location_parent` 映射和对应目录挂载。引导资产
查找与快照兼容性继续使用原生行为。

### 资源池与自定义应用

外层 `CPUQuota`/`MemoryMax` 约束整个实例及其 VM 后代。原生资源池描述可分配的
净预算，需为管理和 Proxy 进程预留空间。部署包 `examples/multi-pool/` 包含
Runner/Builder 映射及 placement 片段；需要时在片段中配置实际宿主 CPU/NUMA
亲和关系。宿主 `MemTotal` 不等于已经确认的容器额度。

部署包 `examples/extensions/` 通过 `paths.conductor_executable` 和
`paths.proxy_executable` 选择 xconductor/xproxy。按基础镜像匹配的 SDK 构建，安装
root 持有的可执行文件，由官方 node-ctl 完成原生 bootstrap。私有配置由应用读取；
环境设置通过实际组件服务上的 systemd `EnvironmentFile` 传入。固定版本的私有
路径可保持 Proxy worker 重启时的输入一致。`node-ctl config` 静态检查不执行
Configure/BindRuntime，需要验证真实自定义应用启动与请求。

## 安装与日常操作

在展开的部署包中，将 `templates/node-infra.container` 复制为实例文件，替换其中
标记的宿主绑定值。保留交付的权限、网络恢复挂载和生命周期钩子。实例额度配置在
`[Service]`；检查前准备引用的原生配置文件和目录。

```sh
sudo ./bin/node-infra-ctl install --name node-a --unit ./node-infra-node-a.container
sudo ./bin/node-infra-ctl check --name node-a --json
sudo systemctl daemon-reload
sudo systemctl start node-infra-node-a.service
sudo ./bin/node-infra-ctl status --name node-a --json

sudo journalctl -u node-infra-node-a.service
sudo podman exec node-infra-node-a journalctl -u node-conductor.service
sudo systemctl stop node-infra-node-a.service
```

Quadlet 从源文件 `[Install]` 段生成开机启动关系。`install` 将宿主工具和钩子固定在
`/opt/node-infra/releases/` 下的版本目录；安装新部署包不会替换其他实例固定的工具。
组件原生配置与宿主实例记录分别维护。

`check` 验证启动输入与未完成恢复。`status` 报告容器、外层服务、各项检查以及
`recovery.pending/error`，并单独提供 `ready` 结果。两者满足其成功条件时返回零，
检查失败/未就绪时返回一。容器运行或 Connector 控制 socket 存在，并不能单独证明
有可分配的执行槽位或沙箱流量正常。完成真实节点就绪和所需业务验收后再接入负载。

## 停止、恢复与更换实例

完整实例停止会结束其中的运行 VM。平台先停止新放置，并完成所需 Pause/Export。
关闭链停止新任务产生，停止并确认全部 Runner/Builder 消费者退出，然后在实例主
网络空间中拆除 Connector。单独停止 Conductor 不会停止全部独立 runner unit；
组件重启保留其原生恢复行为。

启动 PID 1 前，一次性原生 helper 在宿主实例目录中创建 netns pins。系统容器以
`rslave` 传播方式接收这些挂载，容器最终卸载不会移除宿主持有的 pins。
宿主保留实例专属 netns pins、bpffs 入口、已应用镜像/配置身份和恢复记录。正常停止或硬退出后，
宿主钩子在旧容器及 Quadlet 删除完成后执行，使用旧镜像/配置收回原 transit 设备。
失败时保留恢复输入并阻止更换。先通过状态和外层日志确认失败步骤，修复指出的输入，
再重试：

```sh
sudo ./bin/node-infra-ctl status --name node-a --json
sudo ./bin/node-infra-ctl recover --name node-a
sudo ./bin/node-infra-ctl check --name node-a --json
```

恢复检查实际资源结果。不要用同名设备替代尚未归还的 transit，或删除保留的 netns
pins 来消除错误；这些对象标识正在恢复的旧资源。工具管理组件创建的资源，宿主继续
持有主网络空间与已分配设备。

更新时获取并校验目标资产，按需保留业务状态，使用旧输入停止及清理，再安装目标
unit/配置、reload、启动并验证。显式回退使用选定旧镜像与匹配配置，遵循原生状态
兼容规则。镜像更换不转移运行内存，也不改变快照或 migration token 格式。

## 使用发布输入验收

对应 platform 包包含：

- `basic.node-infra-deploy.sh`：正式安装、真实 bare guest、入口、原生 exec、transit、
  cgroup 额度、内存及 E-only 暂停恢复、正常停止。
- `basic.node-infra-config.sh`：原生分层配置、SDK 扩展和私有服务输入、worker 重建、
  两个实际执行池及部分启动失败收尾。
- `basic.node-infra-lifecycle.sh`：两个运行实例、硬退出/恢复、重启及停止隔离。

准备阶段接收同版部署包与原生 OCI 归档；执行阶段将镜像导入 Podman，使用交付的工具
与模板。guest 和扩展 helper 在正常平台 helper 包中预先构建。测试仅创建自身网络
空间、设备与状态，收尾失败时保留恢复输入并报错，不会在系统容器旁另起替代的
Conductor/Proxy。

在 workbench system 模式或目标宿主中，使用公开 E2E runner 的
`--include 'basic.node-infra-*.sh'` 选择用例。缺少输入或能力会明确失败。报告原生架构、
运行器/内核版本及实际通过、失败、跳过的测试；每个支持架构分别提供 KVM 证据。
准备和运行命令见源码[测试快速开始](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/main/test/QUICKSTART.md)，
应用集成契约见[原生 App 文档](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/extensions_zh.md)。
