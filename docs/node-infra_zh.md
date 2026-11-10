[English](node-infra.md) | [简体中文](node-infra_zh.md)

# 部署与运维 node-infra 系统容器

本指南使用 **node-infra** 镜像和对应部署包交付一个 kuasar-sandbox 数据面节点，
负责宿主与容器绑定、安装及实例生命周期。组件原生配置和 API 的完整契约仍由组件仓库
维护。平台节点池路由、宿主设备分配和外部 Cache/Store 服务部署各有其责任方。

## 前置条件与发布输入

使用原生 amd64 或 arm64 Linux 宿主，提供 cgroup v2、可用的 KVM/TUN、网络空间、
BPF/TC 与 userfaultfd。生产使用宿主 systemd 和 rootful Podman/Quadlet；开发可以直接
使用 rootful Docker，包括 cgroupfs driver，无需 Podman 或宿主 node-infra unit。
两种路径均使用同一镜像，由 systemd 担任容器 PID 1。部署包 `release.json` 记录所选支持基线；随包平台 E2E 验证实际模板。
systemd 启动或 generator 检查成功，不能单独作为 VM 与恢复验收通过的证据。

获取同一聚合版本的以下输入：

| 输入 | 用途 |
|---|---|
| `ghcr.io/kuasar-sandbox/node-infra:<version>` | 多架构 OCI 镜像，选择并记录具体平台 manifest。 |
| `node-infra-<version>-linux-<amd64|arm64>.oci.tar.gz` | 对应平台镜像的离线归档，通过 `podman load` 导入。 |
| `node-infra-<version>-linux-<amd64|arm64>.docker.tar.gz` | 同一 OCI 镜像的 Docker 兼容离线归档，通过 `docker load` 导入，由 `node-infra-v2` 提供。 |
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

## 准备默认宿主网络部署

一个宿主部署一个实例、无需专用 transit 出口时，保留模板的 `Network=host`。
无需预建主网络空间、准备特定名称的入口接口或 transit 设备。Conductor 和 Proxy
按原生配置的地址/端口监听，使用宿主现有网络。保持 loopback UP，并提供可用路由、
DNS、KVM/TUN、cgroup 委托及实例私有持久目录。

工具将本地运行器实际使用的网络空间固定在实例私有运行目录中，不假定
`/proc/1/ns/net` 就是该上下文。预检查核对 Podman 选中的网络空间，启动与状态检查
核对主容器和保留句柄的身份。工具与 rootful Podman 必须在同一本地宿主上下文执行。

默认三平面为主网络（Conductor API 与 Proxy 入口）、`sw0-switch`（vSwitch）及
`mgmt`（管理）。Connector 默认使用 `mgmt_extracts: ["mgmt:mgmt0:169.254.169.254/32"]`，
保留 `mgmt_services: ["169.254.169.254:80:127.0.0.1:19254"]`。Proxy 与 Telemetry
使用原生 `proxy_netns: mgmt`；Telemetry 仍需显式启用。

Proxy master 在主网络创建数据监听；worker、后端连接与 MMDS 监听使用 MGMT，
Conductor 经原生控制通道提供 MMDS 策略。客户 xproxy worker 的连接使用 worker 网络
空间；master 扩展创建的连接使用主网络。不要将整个 Proxy service 放入 `NetworkNamespacePath`。

runtime 仅在经过归属验证的管理 peer 上配置服务 VIP 与设备级 `route_localnet`。
Connector 安装按设备限定的浮动地址回程路由。预检查进入实际管理平面（包括借用的网络
空间），允许外部 egress 已有默认路由。默认布局保留宿主原有 metadata VIP、路由和监听；
主网络 API/数据监听冲突仍明确失败。显式旧原生布局继续生效；更换默认布局需要维护重建，
不承诺热迁移或自动改写配置文件。

提供可达的外部 Cache/Store 端点及文件型制品。UDS 挂载提供方持有的父目录，使
socket 重建后仍可见。存储端点不会让任意宿主文件自动在容器内可见，host 网络也
不会自动提供文件访问。

默认 Connector 配置不启用 transit。Proxy 入口、guest Exec 和 MMDS/管理通信仍可用，
但不提供 guest 普通互联网出口。此部署不安装 NAT 或 GENEVE 网关。

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

将 `Image` 设为 `sha256:<64位十六进制配置ID>` 或带摘要的 registry 引用。
即使 `podman image inspect` 返回的配置 ID 没有前缀，也要补上 `sha256:`。
使用预置配置的客户镜像时，移除对应配置的 `Volume` 条目，或只挂载具体站点文件：
挂载整个 `conf.d` 目录会遮蔽镜像内的内容。保留实例的数据与恢复挂载。

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

## 直接使用 Docker 开发

使用相同的不可变镜像和原生配置路径。短时入口识别 Docker/Podman，验证私有 cgroup v2
委托，在缺少时准备私有 bpffs，保护全局 `/proc/sys` 和 `/sys`，随后 exec `/sbin/init`。
它不绑定可写的宿主 cgroup 树，也不引入 supervisor。这是受信管理员的 privileged 系统
容器，不是租户隔离边界。

将 `IMAGE`、`CONFIG_ROOT` 和 `DATA_ROOT` 设置为所选镜像与已准备的绝对路径，包含
四个 `conf.d` 目录和 `manifest.yaml`：

```sh
docker load --input node-infra-VERSION-linux-amd64.docker.tar.gz
docker run -d --tty --name node-infra-dev --pull=never --privileged \
  --cgroupns=private --network=host --cpus=4 --memory=6g --memory-swap=6g \
  --tmpfs /run --tmpfs /run/lock --tmpfs /tmp:exec --stop-timeout=420 \
  -v "$CONFIG_ROOT/node-conductor/conf.d:/etc/node-conductor/conf.d:ro" \
  -v "$CONFIG_ROOT/node-proxy/conf.d:/etc/node-proxy/conf.d:ro" \
  -v "$CONFIG_ROOT/node-telemetry/conf.d:/etc/node-telemetry/conf.d:ro" \
  -v "$CONFIG_ROOT/connector-vswitch/conf.d:/etc/connector-vswitch/conf.d:ro" \
  -v "$CONFIG_ROOT/manifest.yaml:/etc/node-infra/manifest.yaml:ro" \
  -v "$DATA_ROOT:/var/lib/node-infra:rw" "$IMAGE"
docker exec node-infra-dev /usr/libexec/node-infra/node-infra-runtime health
docker logs node-infra-dev
docker exec node-infra-dev journalctl --no-pager
docker stop node-infra-dev
```

bridge 模式将 `--network=host` 换为 `--network=bridge`，发布
`-p 127.0.0.1:23000:3000 -p 127.0.0.1:23001:3001`。三个实例使用独立名称、数据与配置
目录，以及 `23000/23001`、`23010/23011`、`23020/23021` 三组端口。容器 bridge 联网不
代表沙箱获得 NAT。外层额度约束管理进程、资源池与 VM 后代；原生池预算预留管理空间，
原生 CPU/NUMA placement 语义不变。

镜像停止信号为 `SIGRTMIN+3`，运行器的 420 秒预算覆盖内部有序停止，生产外层 unit
另为恢复钩子预留时间。TTY 将 systemd console 接入 `docker logs`。privileged PID
namespace 中的正常 halt 可能返回 130，需同时检查网络 cleanup 完成日志和实际资源结果，
不能只看退出码。强制停止与正常内部清理是不同结果。
`node-infra-ctl health --runtime docker --container node-infra-dev` 仅适配内部健康入口；
`describe` 和 `check` 同样可用。这条 Docker 路径不要求 `node-infra-ctl install`。

## 显式外部管理与 egress 网络空间

将每个实际引用的 nsfs 句柄单独只读绑定到 `/run/node-infra/netns-inputs/<name>`，
不要暴露宿主整个 `/run/netns`。例如先在宿主准备外部 `node-a-egress` 的出口设备、
地址、默认路由、转发与 SNAT，再增加以下 Docker 参数：

```sh
# --mount 在源路径不存在时失败。
--mount type=bind,source=/run/netns/node-a-egress,target=/run/node-infra/netns-inputs/egress,readonly
```

对应 Quadlet 绑定为
`Volume=/run/netns/node-a-egress:/run/node-infra/netns-inputs/egress:ro`。
原生 Connector 覆盖片段如下：

```yaml
mgmt_extracts:
  - "mgmt:mgmt0:169.254.169.254/32"
  - "egress:egress0:0.0.0.0/0"
```

数组整体替换继承列表，保留管理项，兜底项放最后。只导入实际引用的名称；提供的占位
文件、错误 namespace 类型或身份变化均失败。未提供外部输入的命名管理平面由实例
私有创建，不支持接管外部 switch/port 网络空间。已应用记录关联 boot/generation 与
真实句柄。实例只释放本地引用和 Connector 自己的 peer/回程路由；外部已有网卡、地址、
默认路由、SNAT 和网络空间句柄仍归调用者所有。

客户网络设置使用原生 oneshot unit 和服务 drop-in，见部署包 `examples/network-extension/`。
示例仅拥有分配给自己的 nft table，运行在网络准备与 Connector 之间，保留基础
Connector `ExecStartPost` ready 步骤。追加的 post-start 失败会使 Connector 及依赖方
启动失败。官方镜像包含 nftables，并屏蔽会清空全局规则的发行版服务；其他工具由派生镜像构建时提供，不在启动时下载软件。

## 可选的网络空间隔离与 transit

两项选择彼此独立，使用同一模板与工具：host 或显式网络空间，均可有/无 transit。
需要隔离时，只将 `Network=host` 改为 `Network=ns:/run/netns/node-a`。
在 `check` 前准备该保留的 nsfs 挂载，启用 loopback，并在任意适用名称的接口上
配置所需入口地址/路由，例如 `ingress0`。路径必须为规范绝对路径且不能含符号链接。
提供适用 DNS 与 `/etc/resolv.conf`；加入网络空间不会创建它们。宿主重启后，先
恢复这个外部网络空间，再启动实例。

任一主网络模式需要专用 transit 时，明确提供原生片段（随包示例为
`examples/transit/90-transit.yaml`）：

```yaml
# /etc/connector-vswitch/conf.d/90-transit.yaml
transit_dev: uplink0
transit_dev_addr: "192.0.2.6/30:192.0.2.5"
transit_dev_mtu: auto
```

所选设备必须存在于主网络空间，有稳定 MAC，并在接管前处于 DOWN。不得选择
仍承载宿主业务的 UP 接口。Connector 将其移入内部 switch 网络空间，清理时将
同一设备以 DOWN 状态归还。缺省/空 transit 不接管或归还设备，不记录虚假设备身份。
宿主负责设备分配；本部署包不管理 VF 或部署 transit 网关。客户镜像 COPY 与
运行期挂载使用同一片段。

Docker 借入 transit 时，在第一次启动前显式准备保留句柄。这个可选路径接受本地
rootful Docker daemon 中完整、从未启动过的容器 ID，使用原生 host 网络；不借入
transit 的普通开发仍可使用 bridge。保留镜像正常入口，禁用自动重启/自动删除，并
提供三个恢复绑定。使用专用原生 cgroup parent，即使调用方在 `docker start` 后
立即退出，恢复仍可证明旧消费者已经全部结束：

```sh
name=node-a
run=/run/node-infra/instances/$name
state=/var/lib/node-infra/instances/$name
sudo install -d -m 700 "$run/netns" "$run/bpf" "$state/recovery" "$state/data"
parent=/node-infra-$name
if [ "$(docker info --format '{{.CgroupDriver}}')" = systemd ]; then
  suffix=$(printf %s "$name" | sha256sum | cut -c1-16)
  parent=nodeinfra${suffix}.slice
fi
# 添加与直接启动示例相同的原生配置/输入绑定。
id=$(docker create --tty --name node-infra-$name --pull=never --privileged \
  --network=host --cgroupns=private --cgroup-parent="$parent" \
  --cpus=4 --memory=6g --memory-swap=6g --stop-timeout=420 \
  --tmpfs=/run --tmpfs=/run/lock --tmpfs=/tmp:exec \
  -v "$CONFIG/node-conductor/conf.d:/etc/node-conductor/conf.d:ro" \
  -v "$CONFIG/node-proxy/conf.d:/etc/node-proxy/conf.d:ro" \
  -v "$CONFIG/connector-vswitch/conf.d:/etc/connector-vswitch/conf.d:ro" \
  -v "$CONFIG/manifest.yaml:/etc/node-infra/manifest.yaml:ro" \
  -v "$state/data:/var/lib/node-infra:rw" \
  -v "$state/recovery:/run/node-infra/recovery:rw" \
  -v "$run/netns:/run/netns:rw,rslave" -v "$run/bpf:/sys/fs/bpf:rw" "$IMAGE_ID")
sudo ./bin/node-infra-ctl prepare --runtime docker --name "$name" --container "$id"
docker start "$id"
sudo ./bin/node-infra-ctl started --runtime docker --name "$name"
# 正常停止或异常退出后，使用记录的旧 generation。
docker stop "$id"
sudo ./bin/node-infra-ctl recover --runtime docker --name "$name"
```

工具记录精确容器/镜像、本地 daemon、专用 cgroup、namespace generation 和固定的
旧清理工具。恢复按 ID 删除旧的已停止容器后才释放句柄；同名新容器不能重定向清理。
若 daemon 侧 helper create 被中断且结果仍未知，工具保留 journal/句柄并报告不确定
状态。不要删除 pin 或修改 journal 绕过；检查记录的 helper，若 daemon 完成操作后
helper 出现，再重试恢复。如果 helper 始终不存在，同一次 boot 内的恢复可能持续
拒绝：inspect 不存在不能证明待处理的 create 请求已经结束。不存在的 helper intent
只有在 boot 已改变且整个旧运行目录消失后，才能自动退役。

## 停止、恢复与更换实例

完整实例停止会结束其中的运行 VM。平台先停止新放置，并完成所需 Pause/Export。
关闭链停止新任务产生，停止并确认全部 Runner/Builder 消费者退出，然后在实例主
网络空间中拆除 Connector。单独停止 Conductor 不会停止全部独立 runner unit；
组件重启保留其原生恢复行为。

`node-infra-network.service` 拥有 setup 与 `ExecStopPost` 清理，也覆盖部分启动失败。
Connector service 负责进程和基础 ready 步骤。独立 drain unit 停止已应用的全部原生
Runner/Builder 池（含自定义模板），等待真实 cgroup 与 VM 消费者退出。逆序停止为
Proxy/Conductor 等生产者、执行池、Connector、网络清理。组件局部重启不拆 switch 或
停止现有 Guest。只有生产者和消费者均已退出后，才可强制收尾剩余 allocated 记录；
清理失败保留已应用日志。

生产恢复通过短时 helper 在 PID 1 前固定必要网络空间和私有 bpffs。主容器以 `rslave`
接收 pins，内部正常清理拆除对象但保留宿主 pins。旧容器及 helper 退出后，外层钩子
以记录的镜像、工具、generation 和 boot 身份调用同一清理原语，释放自己的绑定。
当前配置不能重定向旧清理目标。旧 schema-1 记录继续使用固定的旧工具与镜像。

SIGKILL、`docker rm -f` 和掉电不能执行内部 teardown。默认无借入设备的 Docker 使用
私有内核对象，业务数据卷不保存活 namespace 句柄。借入 transit 时，需要显式的保留
句柄 prepare/recover 路径保证强杀后准确归还。不能用新建同名网络空间替代丢失句柄。
跨 boot 时宿主单独归档旧日志，不将 JSON 当作活引用。未完成恢复时禁用自动重启。
通过状态和外层日志定位生产恢复失败，修正指出的输入后重试：

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

- `basic.node-infra-deploy.sh`：默认 host/无 transit 与显式网络空间/transit 部署，
  真实 guest Exec/Proxy/MMDS、cgroup 额度、内存及 E-only 恢复、host 硬退出/helper
  中断、资源保护、冲突和另外两种网络/transit 组合。
- `basic.node-infra-config.sh`：原生分层配置、SDK 扩展和私有服务输入、worker 重建、
  两个实际执行池及部分启动失败收尾。
- `basic.node-infra-lifecycle.sh`：两个运行实例、PID 1 和 conmon 硬退出/恢复、配置变化、
  清理冲突与 BPF 元数据丢失后的重试、重启及停止隔离。

`node-infra-v2` 准备阶段验证同版部署包与两种归档，执行阶段分别导入 Podman OCI 和
Docker archive，要求配置及有序 rootfs diffID 相同。`basic.node-infra-docker.sh` 覆盖
直接 host/bridge 启动、组件重启、正常/强制停止、快照和三实例隔离。Podman 路径使用
交付的工具和模板。guest 与扩展 helper 在正常平台 helper 包中预先构建。测试只创建
自己的网络空间、设备和状态；收尾失败保留输入并报错，不在系统容器旁另起替代服务。

在 workbench system 模式或目标宿主中，使用公开 E2E runner 的
`--include 'basic.node-infra-*.sh'` 选择用例。缺少输入或能力会明确失败。报告原生架构、
运行器/内核版本及实际通过、失败、跳过的测试；每个支持架构分别提供 KVM 证据。
准备和运行命令见源码[测试快速开始](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/main/test/QUICKSTART.md)，
应用集成契约见[原生 App 文档](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/extensions_zh.md)。
