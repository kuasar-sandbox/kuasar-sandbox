# deployment — 部署拓扑与组件清单

kuasar-sandbox 平台由若干**独立部署的进程**组成,通过网络协议(gRPC / wire /
vsock / UDS)协作。本文档定义这些进程在生产部署中的归属、责任边界、配置入口与
启停依赖,供运维与 SRE 使用。

各模块的 CLI、配置 schema、内部设计在自己的文档里(`docs/<模块>.md`);本文档
**不**重复这些细节,只回答"东西在哪、彼此怎么找到对方、谁先起谁后起"。

## 1. 角色概览

部署角色按数据路径和控制面拓扑选择.本地文件、共享文件存储与
Manifest/store/cache 可以分别使用,后两者不是单节点或集群部署的强制前提.

**节点与集群角色**

| 角色 | 职责 | 关键进程 |
|---|---|---|
| Compute Node | 承载 MicroVM 和节点资源控制;e2b 模板构建也在本节点的构建沙箱内进行(§5) | `node-ctl serve`(含可选 `resource_listen`;external proxy 模式另启 master + workers)、`sandbox-ctl × N`;Manifest 路径按需部署 `cache-ctl` 和 `store-ctl` |
| Shared Storage | 为命名 `file://` location 提供跨节点原生文件访问 | 部署方提供的 NAS、NFS 或共享文件系统 |
| L2 Cache Cluster(可选) | 为 Manifest 路径提供分布式 EC 缓存,未部署时 cache 可以本地命中或直接回源 | `cache-ctl shard` |
| Cluster Control Plane(可选) | E2B 兼容多节点控制面:registry 维护执行态,router 提供统一入口,placer 导入 group 并放置;生命周期仍由 node 执行 | `cluster-ctl registry`、`cluster-ctl router`、`cluster-ctl placer` |

**外部共享资源**(由部署方运营,平台外)

| 资源 | 用途 |
|---|---|
| Local/NAS/NFS | 本地或跨节点原生文件工件,不要求转换为内容分片 |
| FS 或 S3-compatible store | Manifest 与 chunk 的远程持久化后端 |
| 平台管理面 | 沙箱实例调度、配置、租户管控、模板构建凭据(registry 拉取令牌 / 客户密钥);通过 **cluster 控制面**向 placer/provider 导入 sandbox-group 配置,或在单机部署中直接调用 `node-ctl` e2b API |
| 容器镜像仓库 | 租户镜像来源;构建沙箱内 `flatten-ctl` 按需拉取(OCI v1.1,支持 Referrers) |

## 2. Compute Node

### 2.1 常驻进程

| 进程 | 角色 | 数量 | 启停 | 归属 |
|---|---|---|---|---|
| `node-ctl`(`serve`)| 本机沙箱编排 + e2b 兼容控制面 + 节点级资源仲裁(`resource_listen`)+ node-link 集群接入客户端;经 run-id 模板单元 `sandbox-runner@<run-id>`/`sandbox-builder@<run-id>` 驱动 sandbox-ctl;`proxy.mode=internal` 时还在本进程承载数据面 proxy | 单实例 | systemd | 平台内,`orchestrator/docs/node.md`(资源协议见 node-resource.md)|
| `node-ctl proxy`(`serve`,external 仅)| proxy master 经 config-socket 订阅路由与 conductor-owned MMDS policy、绑定独立数据/MMDS入口并管理 worker;worker 用 mmap 读取固定路由,经 master 本机 RPC 查询可变 MMDS route/value/service,执行数据面鉴权、反代及 native exec gate | 1 master + `workers` 个 worker | systemd | 平台内,`orchestrator/docs/node-proxy.md` |
| `cache-ctl`(`mode: local|tiered`,可选)| Manifest 数据入口:节点本地 L1,可选 EC L2 和 store origin | 每节点至多一个实例 | systemd,使用 Manifest 路径时先于 node-ctl | 平台内,`docs/cache.md` |
| `store-ctl`(可选) | Manifest store 的节点侧 FS/S3-compatible 读写服务 | 每节点至多一个实例 | systemd,使用 Manifest 路径时启动 | 平台内,`docs/store.md` |
| `sandbox-ctl`(`run`) | 单个沙箱的控制平面(类 `runc run`);非 daemon | 每沙箱一个 | 由 node-ctl 经 `sandbox-runner@<run-id>` 单元(`run-sandbox`)assignment 后启动 | 平台内,`docs/sandbox.md` |
| `cloud-hypervisor` | VMM(patched);`sandbox-ctl` 子进程 | 每沙箱一个 | `sandbox-ctl` 派生 | 平台内,`docs/cloud-hypervisor.md` |

### 2.2 端口与套接字

所有节点本机进程默认监听 loopback 或 UDS,不向集群外暴露:

| 进程 | 监听 | 协议 | 用途 |
|---|---|---|---|
| `store-ctl` | `127.0.0.1:7100` | gRPC | `Put` / `Get` / `GetSalt`(本机 cache-ctl + manifest-ctl 调用)|
| `store-ctl` | `127.0.0.1:7061` | gRPC health | 探针 |
| `cache-ctl tiered` | `127.0.0.1:7070` | wire(自定义二进制 TCP)| 数据面:`sandbox-ctl` / `manifest-ctl` 拉 chunk |
| `cache-ctl tiered` | `127.0.0.1:7071` | gRPC | health / `ping` / `info` |
| `node-ctl conductor serve(resource_listen)` | `/run/sandbox-resource.sock` | UDS,自定义协议 | 沙箱资源协议(`sandbox-ctl` 拨号目标)|
| `sandbox-ctl` | `/run/sandbox/<sid>/*.sock` | UDS | sandbox 内部:`ch.sock` / `blk{0,1}.sock` / `uffd.sock` / `ctl.sock` / `vsock.sock`(+ `_5000`);另写 `<sid>.pid`(config-socket 鉴别)、`<sid>.env`(`SANDBOX_ARGS`)|
| `node-ctl`(`serve`) | `api.listen`,如 `:443` | HTTPS/h2 | **对外** e2b 控制面 API;`proxy.mode=internal` 时同一 handler 也承载沙箱数据面,`proxy.data_listen` 可另设数据入口 |
| `node-ctl proxy`(`serve`,external 仅)| `proxy.yaml.data_listen` | HTTPS/h2 或 h2c | **独立数据入口**;master 绑定 listener 并把 fd 交给 workers,native exec 使用 `service=exec` + `X-Access-Token` CONNECT |
| `node-ctl`/external proxy | conductor `mmds.listen`,默认 `127.0.0.1:19254` | HTTP/1.1 | vswitch `--mgmt-service` 的目标;internal 由 conductor 绑定,external 由 master 从可信 Hello policy 取得后绑定并把 fd 交给 workers |
| MMDS local service | `mmds.services.<name>.endpoint` | HTTP/1.1 over UDS | conductor-only registry;V1 为 `unix://` absolute path,internal 直拨,external worker 经 master 取得解析后的 socket path |
| `node-ctl`(`serve`) | `/run/sandbox/node-ctl.socket` | UDS,HTTP/h2c + framed JSON stream | config-socket(run/task/admin/plugin/api 五平面):启动器取 LaunchSpec/BuildSpec;admin 管 manifest key 与 sandbox MMDS value;external proxy/platform agent 经 plugin 平面注册并同步受控路由(SO_PEERCRED + `<id>.pid`/pidfile 鉴别)|

使用 tiered cache 时,EC 客户端通过节点对外网络拨号 L2 cluster 节点的 `7070`
端口(详见 §3);local cache 或直接 store 路径不需要 L2.internal 模式下,conductor
进程内 proxy 与控制面共用 handler;
external 模式下,`node-ctl proxy serve` 启动 1 个 master 和配置数量的 workers,正常数据面流量进入
`proxy.yaml.data_listen`,而控制面仍由 conductor 的 `api.listen` 承载。external worker 使用自身
`proxy.yaml` 中必填的 `paths.run_root` 定位 `<run_root>/<NodeSandboxID>/ctl.sock`;该值是节点本地部署配置,
不经 routesync `Policy` 或共享内存路由视图传递。`proxy.yaml` 不配置 `mmds_listen` 或
`services`;conductor 的 trusted `proxy + route_wake + mmds` registration 是这两项的唯一
投影通道。其余本机进程均使用 loopback/UDS。
`sandbox-ctl` 由 `node-ctl` 经 systemd **模板单元 `sandbox-runner@<run-id>.service`** 拉起
(`StartUnit`/预启动 → 单元内 `run-sandbox` WaitAssignment 后 `execve` 为 `sandbox-ctl run`,非自行 fork-exec)。e2b 模板构建
另走第二个模板单元 **`sandbox-builder@<run-id>.service`**(单元内 `run-builder` WaitAssignment 后
**驻留驱动构建沙箱内的三阶段流水线** import / steps / template,每阶段一台 microVM 作其直接子进程;镜像拉取与 step 执行
都在沙箱内,详见 §5 与 `orchestrator/docs/node.md` §12)。两个模板单元由
`node-ctl conductor serve` 启动时自动生成并安装(`install_units:false` 则交由运维带外管理),完整设计见
`orchestrator/docs/node.md` §5/§12。

运维侧:`/run/sandbox/<sid>/ctl.sock` 除了承载 snapshot,也是 `sandbox-ctl exec
--sandbox-id <sid> -- CMD` 的本机入口.远程调用不会直接暴露该 UDS:客户端先以
`X-API-KEY` 显式申请绑定 AuthSandboxID 的 `kat1` ExecAccessToken,再由
`sandbox-ctl exec --proxy` 和可重复的 `--proxy-header` 透传 SID、`service=exec`、
token 及 cluster context 并建立 CONNECT;最终 node proxy 验证 token 后拨现有
`ctl.sock`,由 `pkg/ctl.ProxyExec` 限制首帧只能是 `exec_request`.远程客户端由
[`sandboxer#28`](https://github.com/kuasar-sandbox/sandboxer/issues/28)交付,也是
standalone,cluster和external-proxy真实guest E2E的必需客户端;这些测试直接调用该客户端,
不再使用临时 CONNECT bridge.完整规格见
`sandboxer/docs/sandbox.md` 和 `orchestrator/docs/node-proxy.md`.

### 2.3 持久化与运行时目录

```
/var/store/                          store-ctl 使用 fs backend 时的数据目录(可位于本地或共享文件系统)
/var/cache/accel-l1/                 cache-ctl local/tiered 的 L1 RocksDB(容量由工作集决定)
/run/node-ctl/                       node-ctl audit + state(tmpfs)
/run/sandbox/<sid>/                  每沙箱运行时目录:socket + snap-stage/snap-state(CH 元数据中转,tmpfs)
/var/lib/sandbox/<sid>/              每沙箱磁盘目录:overlay 写层 <sid>.overlay.diff(本地 NVMe)
```

run 根(`/run/sandbox`,tmpfs)与 base 根(`/var/lib/sandbox`,磁盘)分离:可写
overlay 层必须落盘,不能用 tmpfs。两者分别由 `--run-root`/`SANDBOX_RUN_ROOT`、
`--base-root`/`SANDBOX_BASE_ROOT` 覆盖。快照**产物**另由 `--output` 指定目录(磁盘)。

### 2.4 节点共享资源目录

平台维护节点级共享文件,所有沙箱按名称引用,不复制:

```
/opt/sandbox/
  kernel/
    <ver>/vmlinux                    # 多版本并存,SANDBOX_CONFIG 按名引用
  runtime/
    <ver>/sandbox-runtime.bundle      # 多版本并存
  overlay-templates/
    overlay-1g.ext4                  # 预格式化空 ext4,大小不同的多份
    overlay-4g.ext4
    overlay-16g.ext4
```

引用方式(在 `SANDBOX_CONFIG` 里):

```yaml
boot:
  kernel:  file:///opt/sandbox/kernel/6.1.169-sandbox/vmlinux
  runtime: file:///opt/sandbox/runtime/v1/sandbox-runtime.bundle
  root:
    overlay:
      # diff 省略 → 自动落在 /var/lib/sandbox/<sid>/<sid>.overlay.diff(磁盘)
      diff_template: file:///opt/sandbox/overlay-templates/basic-1G.ext4  # 见下
```

**复制约定**:`sandbox-runtime.bundle` 与 `vmlinux` **不复制**——`sandbox-ctl`
让 CH 以只读 mmap / 直接打开方式使用(DAX 共享 host page cache,N 个沙箱共一份
RAM 工作集)。**overlay 写层**是沙箱独占、运行期被修改的可写盘:`diff` 省略时
`sandbox-ctl` 自动在 base 目录(磁盘)创建,并在 `diff_template` 给定时从模板
**稀疏复制**一份预格式化 ext4(无需 node-ctl 预先 `cp`)。显式给定 `diff`
则按该路径打开既有文件、不复制、不删除。

### 2.5 per-sandbox 配置下发

每个沙箱由 `node-ctl` 在启动单元前写一份 per-sandbox **`SANDBOX_CONFIG`**
(`<sid>.yaml`,非密).使用 Manifest 路径时,再搭配共享的 **`MANIFEST_CONFIG`**
(`manifest.key` 留空)+ per-沙箱 `MANIFEST_KEY` env;本地文件或命名共享文件引用
不要求先建立 Manifest 配置.配置经 `run-sandbox`(单元)以 flag 传入 sandbox-ctl:

| 文件 | 内容 | 传入方式 | 文档 |
|---|---|---|---|
| `SANDBOX_CONFIG` | 该沙箱的资源 / 启动 / 网络 / launch 配置 | `sandbox-ctl run --config <path>`,等价 `SANDBOX_CONFIG` env | `docs/sandbox.md` §3 |
| `MANIFEST_CONFIG`(按需) | Manifest 路径使用的本机 store/cache 端点与内容保护参数 | `sandbox-ctl run --manifest-config <path>`,等价 `MANIFEST_CONFIG` env;`manifest-ctl` 使用同一格式 | `docs/manifest.md` §3 |

要点:

- **per-sandbox 密钥**:每沙箱用各自租户的客户密钥;node-ctl 经**共享**
  `MANIFEST_CONFIG`(`manifest.key` 留空)+ per-沙箱 `MANIFEST_KEY` env 注入(e2b 路径下
  `MANIFEST_KEY` 为该租户内容根密钥——node-ctl 从加密的 APISecret+ManifestKey
  凭据对中解出;**api_key 由 APISecret 签发**,见 `orchestrator/docs/node.md` §7)。
  外部管理面若选择直接对接单机
  `node-ctl`,也必须按同一 per-sandbox 生命周期落地 manifest 配置
- **共享格式**:选择 Manifest 路径时,`manifest-ctl` 与 `sandbox-ctl` 使用同一
  配置格式,并按部署拓扑连接本机 store/cache 端点;命名文件路径不经过该数据面
- **不**走 env、不走全局默认:loader 要求显式 flag 指定路径(详见
  `docs/manifest.md` §3 loader 契约)

### 2.6 MMDS Route 安全与部署边界

MMDS custom route 只在 conductor `mmds.routes.enabled=true` 时受理。租户在 Sandbox
Create 或 Build Register 通过 `X-Kuasar-Sandbox-MMDS`/metadata 声明 exact
static/secret/service route;Header 与 metadata 按 `secrets`、`routes` 两个顶层 key 合并。
admission 后普通 metadata 只保留 canonical routes,initial values 则按 sandbox/build owner
加密存 sqlite。节点本地 admin UDS 可 PUT/DELETE 已声明 name,没有 cluster Secret API。

```text
                              trusted plugin stream
                              routes + values + services
                                      │
guest ─► MMDS VIP ─► internal proxy ──┼─► conductor store + service registry
                    or                │
                    external worker ──┴─► master bounded heap ─► local UDS service
                              MMDS RPC        ▲
                                              └─ conductor-only config
```

service route 固定构造 `GET <exact-path>` over UDS,Host 为 `mmds-service`,只注入
`E2b-Sandbox-Id` 与 `E2b-Sandbox-Service`;不透传 guest Header/query/body,不跟随
redirect。internal 直接使用 conductor registry;external master 从受信 registration 的
Hello policy 原子接收同一 registry,worker 不读第二份 YAML。routesync 断开时 external
MMDS heap 立即 fail closed,完整 Bookmark 后才重新开放。

routes 可随 standalone migration token 的 portable metadata 移动,secret value 不迁移。
只有目标不存在且确实 import 时,standalone CONNECT 可额外注入 secrets-only MMDS 输入;
目标已存在则 token 与 secret 输入都不解析。cluster CONNECT/node-link/placement 不扩展
MMDS contract,也没有 cluster MMDS E2E。

Build Register 的 routes/value 只供本次 builder sandbox。Trigger 不得覆盖;build 终态事务
同时从 build metadata 删除 routes namespace 并删除 value blob,最终 image/template/snapshot
不包含该配置。宿主持久化不会主动把 value 写入 snapshot,但 guest GET 后 plaintext 已进入
guest/application memory,包含内存的 Pause/snapshot 可能捕获该普通 working set;此类制品仍须
按敏感数据保护。

## 3. L2 Cache Cluster

L2 是 Manifest 数据路径的可选加速层.部署可以只使用 local cache + store origin,
也可以按工作集和故障域部署 shard 集群.它不参与本地文件或命名共享文件读取.

### 3.1 集群规格

- **规模**:由工作集、命中率目标、SSD 容量、网络和故障域实测决定
- **编码**:配置 RS 4+1(`data_shards: 4, parity_shards: 1`)时,每个 chunk 编码为
  5 个 shard
- **放置**:Maglev 一致性哈希.每个 chunk 的 5 个 shard 由 chunk hash 通过
  `LocateN(key, 5)` 在配置的 peer 池中确定性选出.5 peer 是该 RS 配置的
  最小集群规模,扩容不改变 placement 算法.详见
  `docs/cache.md` §4.9
- **资源**:SSD、RocksDB BlockCache(`mem_ratio`)和网络规格按部署测量选择
- **隔离**:不同应用域(镜像 chunk / 快照 chunk)可独立部署集群实例,同一套
  软件配置不同 RocksDB path + 不同集群成员
- **持久化**:RocksDB on `/mnt/ssd/accel-l2`,daemon 进程崩溃可热重启不丢数据

### 3.2 端口

| 进程 | 监听 | 协议 | 用途 |
|---|---|---|---|
| `cache-ctl shard` | `0.0.0.0:7070` | wire | shard PUT/GET(由 compute node 上 tiered cache-ctl 发起)|
| `cache-ctl shard` | `0.0.0.0:7071` | gRPC | health / `info` |

`shard` 模式既不访问 L3 也不持有任何 origin 凭据,纯 KV——这是它能水平扩展、
彼此对等无主的前提。

### 3.3 成员变更

集群成员的 `endpoint` 列表写在每个 compute node 上 `cache-ctl tiered` 的
yaml 里(`tiers[].cluster.peers`)。增减节点是 compute node 端的**配置变更
+ SIGHUP**:Maglev 表重算后约 `1/M` 的 `(key, idx)` 迁移到新节点,其他 peer
命中正常。**操作规程:一次只动 1 个 peer**——同时换 ≥ 2 peer 单 key miss 数
可能超过 parity(RS 4+1 parity=1),读路径将 fallthrough origin,正确但慢。

shard 节点本身无须感知集群成员;它只是个 KV。

## 4. 外部持久化与管理资源

### 4.1 存储后端

本地和命名共享文件工件由文件系统直接承载.Manifest 路径的 `store-ctl` 支持
FS 和 S3-compatible 后端:FS root 可以位于本地盘或共享文件系统,S3-compatible
后端可以使用一个或多个 bucket.后端的容量、故障域和共享范围由部署方选择.
store 内部路径由 `store-ctl` 维护,详见 `docs/store.md`.

S3-compatible 后端使用部署配置或 SDK 默认凭据链.凭据只进入可信 host 服务,
不写入 Guest 或文档示例.每个节点可以运行本地 `store-ctl` sidecar 并连接同一
持久化后端;FS backend 也可以直接使用节点本地或共享目录.

### 4.2 外部管理面接口

region 级、独立运营,平台外。与平台的接口:

- 多节点部署:平台管理面向 `cluster-ctl placer` 的 provider/importer 侧导入
  sandbox-group 配置、selector、客户密钥引用和模板构建凭据。
- 单节点部署:平台管理面可直接调用 `node-ctl` e2b API,并按 §2.2 的配置契约
  提供 per-sandbox 启动配置;使用 Manifest 路径时再提供对应内容保护配置.
- 节点侧桥接进程若由外部系统提供,不属于本发布件,也不改变本页列出的进程、
  配置和启动依赖。

构建在 compute 节点的构建沙箱内进行(§5),无独立展平管理面/数据面池。

## 5. 镜像构建(构建沙箱内三阶段)

e2b 模板构建在 compute 节点上进行,**无独立展平池**:每个构建执行绑定一个
`sandbox-builder@<run-id>` 单元(`run-builder` 驻留驱动),镜像拉取与 step 执行都在**构建沙箱(microVM)内**——租户网络
流量与镜像内容不触宿主用户态,宿主侧只做工件接力与收尾上传。完整语义见
`orchestrator/docs/node.md` §12。

### 5.1 三阶段流水

`run-builder` 依 BuildSpec 最多跑三阶段,每阶段一台 `sandbox-ctl run` + cloud-hypervisor
(都计入本单元 cgroup):

| 阶段 | 触发 | 做什么 |
|---|---|---|
| A import | 有 fromImage | 空单盘沙箱 + 单一 `sandbox-runtime.bundle` 内置的 flatten-ctl/mkfs.erofs;guest 内 `flatten-ctl export -` 以租户凭据拉取 + 确定性展平,tarstream 工件经 exec stdio 流回宿主 |
| B steps | 有 steps | base 镜像 + 单一 runtime + 大可写层;**envd 为 app**,RUN/ENV/ARG/WORKDIR/USER 经 envd `process.Start` 执行(与 e2b 同形);导出新镜像工件 |
| C template | 有 startCmd | 生产 e2b runtime 冷启最终镜像;startCmd 经 envd 启动、readyCmd 轮询;`sandbox-ctl snapshot` 出本地快照 bundle |

两类 guest 信道刻意分离:e2b 语义(steps/startCmd/readyCmd)走 **envd**,平台机制(flatten 拉取/
导出、配置注入、工件流回、就绪探针)走 **`sandbox-ctl exec`**。COPY 上下文经对象存储直传
(`builder.files_storage`,presigned PUT/GET,字节不过控制面),构建期 `flatten-ctl tar extract`
解包。

### 5.2 收尾上传(平台凭据唯一出现点)

阶段产物经宿主 workdir 顺序交接;终态:img ⇒ `manifest-ctl store image.img` 后形成
canonical manifest ref;快照 ⇒ 一条 `sandbox-ctl upload-snapshot <bundle>`。未配置
named location 时发布到 manifest;配置 `checkpoint.remote.ref_location_parent` 时发布到
共享文件 location。持久 id 为
`<profile>-<kind>-<base64url(canonical-portable-ref)>`。manifest 模式由本机
`store-ctl`(§2.1 sidecar)承载远端写。

### 5.3 凭据与隔离

租户 registry 拉取凭据仅 `FLATTEN_*` 经 exec env 进入 import 阶段 guest;**`MANIFEST_KEY`
永不入 guest**。凭据来源(任务级 pull token / SDK 明文 / 租户默认 `registry_auth_enc`)由
node-ctl 解析,见 node.md §12。构建池上限由 `sandbox-builder.slice` 的
`CPUQuota`/`MemoryMax` 施加,并发由 `builder.max_concurrent` 准入。

Build Register 的 MMDS initial values 是另一条独立 confidential flow:加密 blob 以 build
owner 落库,运行期只向 synthetic builder Sandbox route 投影,不进 BuildSpec env、普通
metadata、最终 image/snapshot/template;Build Trigger 不接受覆盖,ready/error/cleanup 删除 blob。

## 6. Cluster Control Plane (cluster-ctl)

大规模(多 compute 节点)部署时,机群之上由 **cluster-ctl** 三角色控制面聚合:**registry**
(shardkv 状态集群 + 节点通道枢纽)、**router**(e2b 兼容统一入口:控制面 + 数据面,
按 sandbox-group + route-key + 稳定 sandbox_id 路由,在 node 边界使用 NodeSandboxID;
Exec Session 通过 `Reserve(operation=exec-session)` + `CmdExecSession` 由 node 签发,数据面由
Router 与 node 验证同一 KAT;非 READY route 进入 data Reserve 时,Registry 在触发生命周期
动作前再次验证)、**placer**(group provider/importer、WATCH_LIST 消费方与
放置调度器)。详见 `orchestrator/docs/cluster.md`。单 compute 节点独立部署(直供 e2b SDK)
时**不需要** cluster 层。

```text
client / SDK
    │
    ▼
cluster-ctl router
    │ route_link Reserve/Resolve
    ▼
cluster-ctl registry  ◄──── node_link ────► node-ctl conductor serve × N
    ▲
    │ placer_link Place / verify-key
    ▼
cluster-ctl placer
```

### 6.1 进程

| 进程 | 角色 | 数量 | 启停 | 归属 |
|---|---|---|---|---|
| `cluster-ctl registry` | registry 自聚簇成员;复制 `route_link` / `node_link` / `node_list` / `placer_link` 执行态,承载 node 长连接和 route/node owner RPC | 1 或 N 副本;每个 group/node 由 LocateN 选 owner set | systemd | 平台内,`cluster.md` |
| `cluster-ctl router` | e2b 兼容统一入口(`api.<domain>` 控制面 + 数据面),持近期 route cache;数据面 miss 时 Resolve 并对已知非 READY route 做 data Reserve,create/connect/exec-session 使用对应 Reserve operation;Exec CONNECT 仅替换 stable SID 为 current NodeSandboxID,保持 service/port/token | N 副本(LB 后,无状态)| systemd | 平台内,`cluster-router.md` |
| `cluster-ctl placer` | group provider/importer、WATCH_LIST 消费方与放置调度器;向 registry 提供 PlaceSandbox / PlaceBuild / verify-key | N 副本;按 placer memberlist ready 视图和 group 确定性 failover | systemd | 平台内,`cluster-placer.md` |

小规模可三角色同机共置;大规模按 registry 成员表、router 入口副本和 placer 副本分别扩展。

### 6.2 端口

| 进程 | 监听 | 协议 | 用途 |
|---|---|---|---|
| `cluster-ctl router` | `:443` | HTTPS/h2 | **对外** e2b 控制面 + 数据面入口(机群唯一北向面)|
| `cluster-ctl registry` | `member.listen`,如 `:7700` | JSONRPC over HTTP/h2c 或 HTTPS | 统一控制面;按 path 承载 `/node-link/*`、`/route-link/*`、`/placer-link/*`、`/cluster/membership`、`/internal/registry-member/*`、`/internal/memberlist/*` |
| `cluster-ctl registry` | `node_link.listen`(可选) | JSONRPC over HTTP/h2c 或 HTTPS | 可选独立 node 长连接监听;为空时复用 `member.listen` |
| `cluster-ctl placer` | `placer.listen`,如 `:7800` | JSONRPC over HTTP/h2c 或 HTTPS | placer Place / verify-key API;memberlist HTTP transport 复用同一监听 |

### 6.3 与节点 / 平台管理面的关系

- **节点接入**:每 compute 节点 `node-ctl conductor serve` 配 registry node_link endpoint,拨入 node-link。接入成员
  可以 redirect 到 node owner,或 relay 到首个可用 owner。`node_link` owner 复制完整节点视图;
  `route_link` owner 下发 create/connect/delete/build/key 命令时,通过 node-owner RPC 转给当前
  `link_owner`。
- **平台管理面(平台外)**:向 placer/provider 侧导入 sandbox-group 配置(租户 `manifest_key`、
  `api_secret`、沙箱初始化配置、镜像仓库、模板、nodeSelectors)。registry 不实现 group provider,
  只在 Reserve/Place 冷路径把请求转给 ready placer。凭据对分发是 create/build 前置条件;
  drop 或租约过期不修改已经复制到现有 sandbox/build 记录的凭据对。
- **MMDS 范围**:cluster registry/router/placer/node-link 不新增 MMDS Secret API、CONNECT
  config passthrough、placement 或 admission 语义。MMDS route/value/service 是 compute node
  的 standalone/local proxy contract;通用 metadata 的偶然透传不构成 cluster 支持。
- **成员关系**:registry 成员表由版本化配置分发,通过信号或 API reload。`memberlist` 复用 HTTP 控制面,
  只做 failure detection 和 meta 传播,不维护成员清单,不参与 `LocateN` 分片计算。
- **成员变更**:registry 可同时持有 active / next membership。受影响的 group/node 逻辑 owner set 为
  old/new 并集,提交要求 old quorum + new quorum;node 上报、node_list 投影、group 请求和 placer import/source
  驱动数据自然复制到新 owner set。router/node/placer 通过 `/cluster/membership` 刷新 active/next 视图。

### 6.4 故障域

| 故障 | 影响 | 自愈 |
|---|---|---|
| 单个 `registry` 成员崩溃 | 其参与的逻辑分片降一格;quorum 仍满足时继续服务,不足时该分片停写 | 成员恢复后通过 quorum read / read-repair catch-up;router 本地 cache 使**已建立会话热路径不受影响** |
| registry 整集群完全下电但 shard 数据保留 | 下电期间 Reserve/Place 不可用 | registry quorum 恢复后读取原 shard 数据,node-link 重连并继续收敛 |
| registry 执行 shard 不可恢复地丢失 | 不得从备份构造 sandbox/build 节点执行态 | provider 数据按其持久化流程恢复;存活 node 的执行投影恢复和 migration-token 持久 route 分别由 [orchestrator #34](https://github.com/kuasar-sandbox/orchestrator/issues/34)/[#33](https://github.com/kuasar-sandbox/orchestrator/issues/33) 跟踪,完成前须明确报告不可恢复 |
| `router` 崩溃 | 该副本连接断 | 无状态,LB 改路由其余副本 |
| `placer` 崩溃 | 该 placer 不再作为 ready 候选;冷放置 failover 到同 group 的其他 placer | 热路径不受影响;Place 超时后 registry 换下一个候选 |
| 单 compute 节点 node-link 失联 | registry 暂失该节点视图 | 节点重连重报;node_dead_after 后 node_list 失效,placer 不再放置到该节点;孤儿 sandbox 按 group+sandbox_id 清理 |

## 7. 全景拓扑

按节点角色分三张子图。每张图自闭合:外部端点用 `(...)` 标注,实体在其他
子图或 region 级。

### 7.1 Compute Node

```
   ┌─ Compute Node ───────────────────────────────────────────────────────────────────────────────────┐
   │                                                                                                  │
   │   ── process tree ──                                                                             │
   │                                                                                                  │
   │   (Platform Mgmt Plane, region)                                                                  │
   │           │  per-sandbox SANDBOX_CONFIG / optional MANIFEST_CONFIG                              │
   │           ▼                                                                                      │
   │   node-ctl            ── StartUnit ──►  sandbox-ctl × N    ── spawns ──►  cloud-hypervisor       │
   │                                                │                                  │              │
   │                                                │                                  ▼              │
   │                                                │                              guest VM           │
   │                                                │                                                 │
   │                                                └── UDS  /run/sandbox-resource.sock ──► node-ctl  │
   │                                                                                                  │
   │   ── data path ──                                                                                │
   │                                                                                                  │
   │   sandbox-ctl ──► Local File / NAS / NFS                                                         │
   │        │                                                                                         │
   │        └── optional Manifest ObjectGet :7070 ──► cache-ctl local/tiered                          │
   │                                                  │   L1 RocksDB                                  │
   │                                                  ├── wire EC fan-out (5 shards) ──► (L2 cluster) │
   │                                                  └── origin gRPC :7100 ──► store-ctl  (sidecar)  │
   │                                                                                       │          │
   │                                                                                       ▼  HTTPS   │
   │                                                                      (FS / S3-compatible store)  │
   │                                                                                                  │
   └──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 7.2 L2 Cache Cluster

```
                            wire EC fan-out  (5 shards / chunk)
                            from every Compute Node's cache-ctl tiered
                                                │
                                                ▼
   ┌─ Optional L2 Cache Cluster ──────────────────────────────────────────────────────────────────────┐
   │                                                                                                  │
   │      cache-ctl  shard      wire :7070   /   gRPC :7071                                           │
   │      RocksDB on SSD                                                                              │
   │      Maglev placement:   LocateN( chunk_hash, 5 )  over full peer pool   (RS 4+1)                │
   │                                                                                                  │
   │      no origin credentials here — origin access stays in each Compute Node's store-ctl           │
   │                                                                                                  │
   └──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 7.3 Image Build (in-sandbox, on Compute Node)

```
   ┌─ Compute Node ── e2b template build  (§5) ───────────────────────────────────────────────────────┐
   │                                                                                                  │
   │   node-ctl          ── assign ──►  sandbox-builder@<run-id>  ( run-builder, resident )            │
   │                                              │  drives 3 stage VMs (sandbox-ctl run + CH)         │
   │                                              ▼                                                    │
   │     A import ──► B steps ──► C template      ( guest: flatten-ctl / envd; tenant net stays in VM )│
   │                                              │  image.img / snapshot bundle  (host workdir)       │
   │                                              ▼                                                    │
   │   publish ──► local/named file location, or Manifest ──► store-ctl ──► (FS / S3-compatible store) │
   │                                                                                                  │
   └──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

构建复用 compute 节点既有的 vswitch 网络槽(guest 拉取出网).选择 Manifest 发布时
复用 `store-ctl`;选择本地或命名共享文件 location 时直接发布文件.两种路径都无
独立构建池或额外常驻进程(§5).

## 8. 启停依赖

### 8.1 启动顺序

**外部依赖(按部署选择)**

1. 本地/共享文件路径已挂载并可访问;若使用 Manifest,对应 FS 或 S3-compatible
   store 后端已就绪

**L2 Cache Cluster(可选,在使用它的 compute 之前)**

2. 配置的 `cache-ctl shard` 成员启动并健康
3. 集群成员清单(`tiers[].cluster.peers`)落到 compute node 配置仓

**Compute Node(每节点独立)**

4. 使用 Manifest 时启动 `store-ctl`,确认 active generation 已 init 且 gRPC 健康
5. 使用 cache 时启动 `cache-ctl local|tiered`;tiered 模式确认 L2 peer 和 store
   origin 可达
6. `node-ctl conductor serve`(`resource_listen`)→ state 恢复或冷启;e2b SDK
   直连或 cluster registry 接入后开始接受新沙箱

注:`cache-ctl tiered` 启动**不需要**等 L2 全员在线——tier chain 把瞬时
故障层视作 miss 下穿(详见 `docs/cache.md` §错误模型)。**写**路径
(`manifest-ctl store` 直连 `store-ctl`)在所选持久化后端 / store-ctl 不可达时会失败.

模板构建复用 compute 节点的 `node-ctl`;选择 Manifest 输出时再复用 `store-ctl`,
无独立启停依赖.`node-ctl` 与所选数据后端就绪后即可经 e2b API 接受构建(§5).

### 8.2 关闭顺序(自顶向下)

1. 平台管理面 / cluster / 客户端停止向该节点 `node-ctl` 调度新沙箱
2. `node-ctl` 等待存量沙箱自然退出 / 主动 snapshot,然后 SIGTERM,persist 状态(含资源预算)后退出
3. 若部署 `cache-ctl`,则 SIGTERM,等待 in-flight 请求结束和 RocksDB flush
4. 若部署 `store-ctl`,则最后停止该服务

L2 cluster 的关闭与 compute node 关闭无强序——每个 compute node 的 `cache-ctl
tiered` 自己处理 L2 不可达。

## 9. 故障域

| 故障 | 直接影响 | 自愈 |
|---|---|---|
| 单 compute node `store-ctl` 崩溃 | 本机 Manifest origin read/write 停;L1/L2 命中和文件路径不受影响 | systemd 重启后恢复 origin 访问 |
| 单 compute node `cache-ctl tiered` 崩溃 | 本机沙箱新 fault 卡 wire dial | systemd 重启;RocksDB 持久化 ⇒ L1 命中不丢 |
| 单 `cache-ctl shard` 节点崩溃 | RS 4+1 容 1 节点故障;L2 仍服务 | systemd 重启;tiered 端 Maglev 表在该 peer 不可达期间把请求路由到其余 4 + 1 parity |
| 同 RS 组中 ≥ 2 `cache-ctl shard` 同时崩溃 | 部分 `(chunk, idx)` 落到 ≥ 2 故障 peer 上,该 chunk L2 miss | 读路径 fallthrough origin(慢但正确);避免方式:成员变更**一次只动 1 peer** |
| `node-ctl` 崩溃 | 北向 API 中断,新沙箱无法拉起 / admit 失败;存量沙箱保持上次 grant 继续跑(资源仲裁随进程在本机) | systemd 重启;状态在 sqlite(`db_path`,默认 `<base_root>/node-ctl.db`)持久化 + 资源 `state.json` 在 tmpfs(扫 cgroup 重建),以 `ListUnitsByPatterns("sandbox-runner@*.service")` 的存活单元对账 sqlite `sandboxes` 表重挂(active+running⇒adopt 重武装 TTL;running 无单元⇒标 dead;`paused`/snapshot 记录保留可被 connect/auto-resume 拉起) |
| S3-compatible 后端不可达 | 使用该 origin 的 Manifest 读写受影响 | 已 L1/L2 命中的沙箱继续跑;依赖新 origin 的写路径 / cold-image fault / 展平上传失败;本地或共享文件路径不受影响 |
| compute node 整机故障 | 该节点运行中的沙箱中断 | 平台隔离故障节点;已发布到命名共享文件或 Manifest 的暂停工件可由 cluster 在其他节点导入恢复,仅本地工件仍依赖原节点 |
| L2 cluster > parity 同时故障 | 相关 L2 读取 miss | tiered fallthrough origin(slow path 持续);恢复后自然恢复 |

## 10. 部署规模示例

### 10.1 开发 / PoC(单机)

```
单机:  store-ctl       (fs backend, /var/store)
       cache-ctl       (mode: local,无 L2)
       sandbox-ctl × N (node-ctl 可省,手工 run)
```

无 L2 cluster、无远程对象存储、无独立资源控制器(资源仲裁随 `node-ctl conductor serve` 内置,`resource_listen`
未配则静态 cgroup);`manifest-ctl` 走本机 `store-ctl` + `cache-ctl`。对应 `docs/cache.md`
§3.2 (local 模式)。**单机直供 e2b SDK,无需 cluster 层(`node-ctl conductor serve` 即北向面)**。

### 10.2 生产单 AZ

```
Compute:           按峰值 working set、活跃比例、恢复成本和安全余量扩展
Storage:           Local NVMe / NAS / NFS / FS or S3-compatible store
Optional cache:    local cache,或按命中率与故障域扩展的 L2 shard cluster
Control plane:     node-ctl standalone,或 registry + router + placer
```

每个 compute 节点运行 `node-ctl` 和当前沙箱对应的 `sandbox-ctl`;使用 Manifest
数据路径时再部署 `store-ctl` 与可选 `cache-ctl`.节点数量、单节点并发和 cache
容量必须用目标版本、硬件、沙箱规格与 workload 实测,不能由架构图中的固定值推导.
Cluster Control Plane 由 registry 自聚簇、LB 后的 router 和 placer 组成,副本数按
可用性与负载选择.

### 10.3 多 AZ

每个 AZ 可以独立运行 compute 和可选 L2 cache,并按故障域选择共享文件系统或
S3-compatible store.使用 tiered cache 时,compute 节点通常优先连接本 AZ peer,
减少跨 AZ 热路径流量.是否跨 AZ 共享内容由内容密钥、安全域和后端配置共同决定,
不能仅因内容相同自动跨租户或跨故障域共享.

## 11. 配置入口速查

每个模块的完整 yaml schema 在自身文档里,本节只给入口指针。

| 进程 | 配置位置 | 部署惯例 | Schema 文档 |
|---|---|---|---|
| `store-ctl` | `--config <path>` | `listen: 127.0.0.1:7100`(节点本机)| 源仓 `accelerator/docs/store.md` §3;发布包 `docs/store.md` |
| `cache-ctl tiered` | `--config <path>` | `listen: 127.0.0.1:7070`(节点本机);`tiers[].cluster.peers` 写所选 L2 成员 | 源仓 `accelerator/docs/cache.md` §3.4;发布包 `docs/cache.md` |
| `cache-ctl shard` | `--config <path>` | `listen: 0.0.0.0:7070`(对外服务)| 源仓 `accelerator/docs/cache.md` §3.3;发布包 `docs/cache.md` |
| `node-ctl conductor serve` | `/etc/node-ctl/conductor.yaml` | `mmds.listen/routes/services` 是 MMDS 唯一配置源;service 仅 `unix://` absolute path | 源仓 `orchestrator/docs/node.md` §3/§4.6;`node-proxy.md` §7 |
| `node-ctl proxy serve` | `/etc/node-ctl/proxy.yaml` | external data listener/worker/shm bootstrap;不重复配置 MMDS listen/services | 源仓 `orchestrator/docs/node-proxy.md` §2 |
| `node-ctl conductor serve(resource_listen)` | `/etc/node-ctl/conductor.yaml` 的内联 `resource_listen` 块 | `socket: /run/sandbox-resource.sock` | 源仓 `orchestrator/docs/node-resource.md` §3;发布包 `docs/node-resource.md` |
| `cluster-ctl registry` | `--config /etc/cluster-ctl/registry.yaml` | `member.id/listen`;`membership.active/versions[].members[].advertise/node_advertise/owners`;`node_link`、`route_link`、`node_list`、`placer_link` | 源仓 `orchestrator/docs/cluster.md`;发布包 `docs/cluster.md` |
| `cluster-ctl router` | `--config /etc/cluster-ctl/router.yaml` | `registry.bootstrap` 指向 registry 控制面;router `:443`(LB 后 N 副本);请求必须带 `X-Kuasar-Sandbox-Group` | 源仓 `orchestrator/docs/cluster-router.md`;发布包 `docs/cluster-router.md` |
| `cluster-ctl placer` | `--config /etc/cluster-ctl/placer.yaml` | `placer.id/listen/advertise/memberlist_label`;`registry.bootstrap`;`import_groups[]`;`placement` | 源仓 `orchestrator/docs/cluster-placer.md`;发布包 `docs/cluster-placer.md` |
| `sandbox-ctl run` | `--config <path>`(`SANDBOX_CONFIG`);Manifest 路径另加 `--manifest-config <path>` | **per-sandbox**,由 `node-ctl` 生成,落在 `/run/sandbox/<sid>/` | 源仓 `sandboxer/docs/sandbox.md` §3;发布包 `docs/sandbox.md` |
| `manifest-ctl` | `--manifest-config <path>`(`MANIFEST_CONFIG`)| 与 `sandbox-ctl` 共享 Manifest 配置和 store/cache 端点 | 源仓 `accelerator/docs/manifest.md` §3;发布包 `docs/manifest.md` |
| `flatten-ctl` | CLI flag + `--manifest-config`(`MANIFEST_CONFIG`,`--upload` 时)+ `--config`(`FLATTEN_CONFIG`,registry 源时);凭据走 `FLATTEN_REGISTRY_*` env | 经单一 guest runtime 在构建沙箱 guest 内运行(`run-builder` 驱动,§5)| 源仓 `guest-runtime/docs/flatten.md` §2;发布包 `docs/flatten.md` |

构建产物路径、跨架构和独立/聚合发布见 `kuasar-sandbox/README.md`
与 `kuasar-sandbox/docs/release.md`;发布包解压与测试入口见
`test/QUICKSTART.md`;性能基线、回归 checklist 见 [`perf.md`](perf.md)。

## 12. See Also

- [`docs/kuasar-sandbox.md`](kuasar-sandbox.md) — 系统设计总览:业务目标、子系统分工、端到端数据流
- `sandboxer/docs/sandbox.md`(发布包:`docs/sandbox.md`) — compute node 上 `sandbox-ctl` 的完整生命周期
- `accelerator/docs/cache.md`(发布包:`docs/cache.md`) §3.1 — `local` / `shard` / `tiered` 三形态选择;§4.9 Maglev 一致性哈希
- `accelerator/docs/store.md`(发布包:`docs/store.md`) — 后端选择(FS / S3-compatible)与内部存储组织
- `orchestrator/docs/node-resource.md`(发布包:`docs/node-resource.md`) — 节点资源控制协议
- `orchestrator/docs/node.md` — e2b 兼容控制面与节点主机;`cluster.md` — 集群级注册表 / 路由 / 放置
- `accelerator/docs/manifest.md`(发布包:`docs/manifest.md`) — `MANIFEST_CONFIG` 格式与 loader 契约
