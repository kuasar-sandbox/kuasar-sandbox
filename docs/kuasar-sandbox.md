# kuasar-sandbox - MicroVM 沙箱平台系统总览

Kuasar Sandbox 面向 Agent,Serverless,Code Interpreter 和强化学习等需要隔离执行,
快速实例化,长会话状态与弹性资源治理的工作负载.系统以独立 Guest Kernel 作为沙箱
边界,支持从快照模板创建新实例,也支持暂停并恢复同一个逻辑实例.

本文描述公开系统语义和组件边界.组件内部协议,字段布局和存储编码由各组件仓文档
维护;实现状态以组件最新 `main`,聚合版本选择和对应 Release 资产为准.

## 1. 概述

### 1.1 用户场景

Kuasar Sandbox 解决以下共同问题:

- Agent 和 Code Interpreter 需要为每个会话提供独立内核与文件系统状态,同时避免
  每次从空环境完成全部初始化.
- Serverless 和并行任务需要从同一个已准备环境创建多个身份独立的实例.
- 强化学习 rollout 需要并行复制统一初始环境,并让每条轨迹独立演进.
- 长会话会在模型调用,人工确认或外部事件期间等待,此时应释放空闲资源,必要时暂停
  实例,之后继续同一个逻辑会话.
- 单节点和集群都需要明确的资源准入,恢复,网络身份和数据可移植边界.

系统能力面向生产部署.公开发行通道的 Stable 或 Preview 状态描述资产与接口的发布
稳定性,不替代部署方对容量,可靠存储,TLS,网络策略和凭据管理的生产配置.

### 1.2 设计原则

1. **隔离优先**:每个沙箱拥有独立 Guest Kernel;共享发生在明确的只读层,可信 host
   数据路径和配置的安全域内.
2. **用户语义稳定**:区分"从模板创建新实例"和"恢复同一个实例",不把上层容量准备
   策略当作另一种快照格式.
3. **数据路径可选择**:本地文件,共享文件存储和 Manifest/对象存储/缓存都是有效
   后端,部署方按可移植性,规模和基础设施选择.
4. **控制与执行分层**:节点控制器负责准入和共享池,sandboxer 负责单沙箱资源执行;
   集群 placer 负责放置,不接管沙箱生命周期.
5. **事实与目标分离**:实现状态,测试结果和设计目标分别标注;性能数字必须携带可复现
   的测试口径.
6. **组件独立**:五个组件仓可独立演进,部署和发布,主仓只承载系统设计,跨组件验证和
   聚合发布.

### 1.3 系统边界

Kuasar Sandbox 提供 MicroVM 生命周期,数据访问,节点网络和单节点/集群编排.它不替代:

- 外部身份系统,租户计费,业务工作流和全局容量规划;
- 对象存储,共享文件系统,OCI Registry 或外部策略网关本身;
- 应用内部的内存限制,错误处理和数据分级;
- 由 Issue 或 RFC 描述但尚未合入并发布的能力.

## 2. 用户入口与部署配置

### 2.1 用户入口

单节点部署由 `node-ctl conductor serve` 提供 E2B 兼容控制面和数据面入口.多节点部署由
`cluster-ctl router` 提供统一入口,通过 registry 和 placer 将 group-scoped 请求路由到
目标 node.两种模式都支持未修改的 E2B SDK 所使用的创建,执行,暂停,连接/恢复和销毁
语义.

`sandbox-ctl` 是节点内部的单沙箱执行工具,负责启动 MicroVM,执行,快照,恢复和工件
导入/发布.普通平台用户通过 E2B 或平台 API 使用系统,不需要直接操作内部 socket 或
组件协议.

### 2.2 配置归属

配置由拥有该行为的组件维护:

| 范围 | 配置与事实来源 |
|---|---|
| 单沙箱启动,磁盘,快照和资源执行 | `sandboxer/docs/sandbox.md` |
| 单节点 API,凭据,构建和恢复 | `orchestrator/docs/node.md` |
| 节点准入和共享资源池 | `orchestrator/docs/node-resource.md` |
| 集群 registry/router/placer | `orchestrator/docs/cluster*.md` |
| Manifest,store 和 cache | `accelerator/docs/{manifest,store,cache}.md` |
| vSwitch 和网络身份传递 | `connector/docs/vswitch.md` |
| Guest runtime 和 kernel | `guest-runtime/docs/*.md` |

部署拓扑,进程依赖和端口见 [deployment.md](deployment.md).顶层文档不复制组件内部
字段或 wire contract,避免用旧设计推演替代当前实现.

## 3. 系统架构

### 3.1 总体拓扑

```text
Agent / E2B SDK / Platform API
                │
        orchestrator
                │
        sandboxer runtime
        ├── accelerator
        ├── connector
        └── guest-runtime
                │
KVM / Local File / NAS / Object Storage / Network
```

### 3.2 组件职责

| 组件 | 系统职责 |
|---|---|
| `orchestrator` | `node-ctl` 提供单节点 E2B 兼容服务,节点准入和恢复;`cluster-ctl` 的 registry,router,placer 提供 group-scoped 集群控制面 |
| `sandboxer` | 驱动 Cloud Hypervisor 和 Guest,执行 MicroVM 创建,运行,快照,恢复,销毁和单沙箱资源闭环 |
| `accelerator` | 为镜像,快照和稀疏工件提供本地/远程引用,Manifest,store,cache,OCI 拉取与 EROFS 展平能力 |
| `connector` | 提供 eBPF/TC vSwitch,TAP 交接,网络资源生命周期,沙箱隔离和外部网关集成基础 |
| `guest-runtime` | 构建并发布 Guest runtime 镜像和 Guest kernel 两个发布单元 |

主仓不复制这些实现.它维护系统级设计,跨组件真实 MicroVM E2E,公共执行环境和精确
版本组合的聚合 Release.

### 3.3 依赖边界

组件依赖保持薄而明确:

```text
T0: accelerator, connector, guest-runtime/native-deps
T1: sandboxer -> accelerator/pkg/{manifest,image} + connector/pkg/tapfd
T2: orchestrator -> sandboxer/pkg/resource + accelerator
```

重实现留在组件内部,跨仓只导出具体包.五个组件既可组合为完整平台,也可按场景独立采用.

## 4. 快照与实例语义

### 4.1 从快照模板创建新实例:1:N

模板在发布前完成环境,依赖和可选应用进程的初始化.多个创建请求可以引用同一个只读
模板父层,但每个实例获得独立 Sandbox ID,网络身份,凭据绑定和增量状态.后续写入,
进程推进和暂停只属于该实例,不会反写模板或其他实例.

这种语义适用于 Agent,Code Interpreter,RL rollout,Serverless 和并行任务:共同的
初始化成本由模板承担,每个实例从统一起点独立演进.

### 4.2 暂停并恢复同一个实例:1:1

暂停保留同一个逻辑实例的进程,内存和文件系统状态.连接/恢复继续使用稳定的公开
Sandbox ID.工件保留在原节点时可以原地恢复;工件发布到命名共享文件位置或 Manifest
后,集群可以在其他节点导入并恢复,节点内部执行 ID 可以变化,公开身份不变.

这种语义适用于等待模型调用,人工确认,外部事件,长会话和节点迁移.预热池
(`warm pool`)只是一种上层资源准备策略,可以提前维持可用实例或模板,不是独立的
快照技术路线.

### 4.3 分层快照

快照复用来自明确的父层关系:

```text
template parent (read-only)
        ├── instance A delta
        │       └── later pause delta
        └── instance B delta
                └── later pause delta
```

- 同一模板创建的实例共享模板父层,各自只维护增量.
- 后续暂停可以把当前磁盘和内存状态作为新层,并显式引用先前父层.
- 磁盘工件与内存快照分别记录来源,恢复时校验完整父链.
- 本地,命名共享文件和 Manifest 都承载同一层级语义,载体不改变用户身份模型.

共享效率不依赖不同虚机运行后偶然产生相同内存字节.可复用内容来自明确模板父层,
不可变 runtime/rootfs 的 host page cache,以及安全域内稳定工件的内容组织.

## 5. 数据路径与 accelerator

### 5.1 统一引用语义

上层生命周期使用统一的工件引用和完整性信息,而不是要求所有数据先转换成同一种后端.
当前有效路径包括:

1. **本地文件**:适合单节点,本地 NVMe 和节点亲和工作负载.镜像,磁盘层和快照可以
   保持原生文件形态.
2. **共享文件存储**:NAS,NFS 或其他共享文件系统提供跨节点原生文件访问.命名文件
   location 可以直接承载快照和镜像,不要求先转换为内容分片.
3. **Manifest / 对象存储 / 分层缓存**:适合大规模分发,远程持久化,跨节点恢复,
   按需读取和热点缓存.工件不依赖原节点文件;store 后端可以是文件系统或
   S3-compatible 对象存储.

部署方可以为不同工件选择不同路径.例如,节点本地临时状态使用 NVMe,共享 NAS 快照
直接使用文件位置,需要广泛分发的镜像使用 Manifest 与分层缓存.

### 5.2 accelerator 定位

`accelerator` 是面向镜像,快照和稀疏工件的数据访问与存储基础组件.它覆盖:

- 普通与稀疏数据表示,并严格区分 Hole,Zero 和 Data;
- 本地工件,稀疏区域处理,完整性校验和可选加密;
- 本地文件,共享文件和远程内容引用;
- 内容组织与 Manifest;
- 本地/共享文件系统和 S3-compatible store 后端;
- 本地缓存,分布式缓存和回源组成的分层缓存;
- OCI 镜像拉取,确定性 EROFS 展平和 runtime config 投影;
- 按需读取,working-set 加载和显式预取.

内容寻址和去重是其中一种能力,不是 accelerator 的唯一价值.同模板实例首先依赖
父层共享;内容级复用更适合稳定镜像,runtime 和高重复工件,并受内容密钥与安全域
边界约束.

### 5.3 稀疏与按需数据

稀疏语义是三态:Hole 来自权威元数据,Zero 表示逻辑零数据,Data 表示实际内容.系统
不会扫描零字节来创造 Hole.快照和磁盘层可以按引用范围读取,cache 命中与预取减少
远端访问,但不改变工件的完整性和父层关系.

## 6. 高密资源治理

Agent 沙箱通常长期等待,短时突发.系统按以下闭环提高节点有效资源利用率:

```text
long wait, short burst
        │
        ▼
release idle CPU and inactive memory
        │
        ▼
pause long waits to snapshots and return resources to the node pool
        │
        ▼
admission + dynamic budget + watermarks + reclaim + safety margin
        │
        ▼
protect active work and safely host more logical sandboxes
```

职责分层如下:

| 组件 | 职责 |
|---|---|
| `sandboxer` | 执行单沙箱资源配置,协同 Guest Balloon,host Cgroup 和 VMM 生命周期,完成增长,收缩,暂停与恢复 |
| `node-ctl` Reservation Controller | 管理节点准入,共享资源池,水位,Grant,Inventory,恢复和节点安全余量 |

节点控制器不读取或修改沙箱 Cgroup,也不直接调用 VMM Balloon;它只通过资源协议管理
Reservation 和 Grant.增长先取得节点 Grant 再扩大沙箱预算;收缩先由单沙箱闭环收敛,
再释放 Reservation.
长时间不活跃的实例可以暂停,把执行资源归还节点池.

平台保障边界是:在沙箱声明资源和节点准入模型范围内,避免节点级资源复用导致沙箱
OOM 和有效工作丢失.应用仍可能因为自身资源声明,内存上限或行为发生 OOM,平台不作
无条件保证.

高密度是节点资源利用率提高后的结果,而不是孤立追求实例数量.容量必须由实际 workload
的峰值 working set,活跃比例,恢复成本和节点安全余量共同验证.

## 7. 网络与多租边界

### 7.1 网络目的

`connector` 为沙箱网络提供以下基础:

- 快速分配,恢复和回收沙箱网络资源;
- eBPF/TC 内核态高速转发;
- 沙箱之间默认隔离,不提供任意端口间直通;
- 平台生成并验证可信沙箱网络身份,不信任 Guest 自报身份;
- 将可信身份传递给外部策略网关;
- 为沙箱级公网,私网,DNS,代理和审计策略提供集成基础.

顶层系统只定义这些功能目的.网络位置编码,封装字段和 option 布局属于 connector
组件文档,不是用户首要接口.

### 7.2 功能状态

| 能力 | 状态 | 说明 |
|---|---|---|
| 基础 vSwitch,端口生命周期和沙箱隔离 | 已交付 | 已由 connector 主线和组件 E2E 维护 |
| 管理服务路径与外部网关接入基础 | 已交付 | vSwitch 提供可信入口/出口和身份传递基础 |
| 集中式策略网关 | 支持集成 | 由部署方的外部网关执行公网,私网,DNS,代理和审计策略 |
| 节点本地轻量 Egress | Proposed | 仅为 [`connector#9`](https://github.com/kuasar-sandbox/connector/issues/9) 设计提案,不是当前 vSwitch 已交付能力 |
| OpenTelemetry | Proposed | 仍处于 [`kuasar-sandbox#52`](https://github.com/kuasar-sandbox/kuasar-sandbox/issues/52) 和在途实现阶段,不是当前部署前提 |

`已交付` 表示代码已合入组件主线并有组件验证,不等同于聚合 Stable Release 已经发布.
`Proposed` 不因存在 Issue,RFC 或 PR 自动成为已交付能力.

### 7.3 多租安全

- 平台身份凭据与内容保护密钥用途分离.APISecret 用于平台身份和控制面验证,
  ManifestKey 保护内容密钥域.
- 控制面,普通数据面和远程执行使用不同作用域的访问凭据;远程执行 token 绑定
  具体 subject,用途和条件.
- 内容保护密钥留在可信 host 数据路径,不注入 Guest.
- 本地和远程工件可以按部署策略加密和校验;密钥保护,备份和轮换仍是部署责任.
- 去重和共享范围由内容密钥与安全域配置决定.内容相同不代表应该跨租户共享.
- 暂停,恢复和迁移后,稳定 Sandbox ID,凭据 binding 和可信网络身份边界继续成立.
- 网络隔离,节点资源准入和故障域与数据加密共同构成多租安全,不能由单一机制替代.

凭据更新只影响之后创建并复制凭据的业务记录.已持久化到现有沙箱记录的凭据保持其
原 binding,不会因 key-distribution 表更新自动重绑.

## 8. 集群与可靠性

### 8.1 单节点和集群

单节点由 `node-ctl conductor serve` 管理本机沙箱,构建,数据面代理和可选 Reservation Controller.
集群由三个独立 `cluster-ctl` 角色组成:

- `registry`:维护 node,route 和 placer 的可靠执行态;
- `router`:提供 E2B 兼容统一入口,按 sandbox-group 路由;
- `placer`:导入 group 配置并选择候选节点,不拥有沙箱生命周期.

生命周期仍由目标 node 和 sandboxer 执行.公开 Sandbox ID 在同节点恢复,跨节点迁移和
重新放置时保持稳定;节点内部执行 ID 可以随 placement generation 变化.

### 8.2 恢复与故障域

- `sandbox-ctl`,VMM,node,registry,router 和 placer 是可独立观察和恢复的进程/故障域.
- 节点持久记录与运行态 Inventory 用于重启恢复和孤儿清理;host socket 存在本身不是
  Guest ready 证据,恢复必须通过实际健康或执行操作确认.
- 本地工件支持节点亲和恢复;共享文件或 Manifest 工件支持脱离原节点的导入和恢复.
- registry 保存集群执行态,node 是运行中 sandbox 的事实来源;placer 只给出放置建议,
  最终资源确认仍在 node admission.
- group 是集群分区键,路由,放置,凭据验证和操作保持 group-scoped.

### 8.3 版本与验证

组件有独立版本和 Release,主仓聚合版本选择精确的六个发布单元:四个组件 archive,
Guest runtime 和 Guest kernel.聚合准备校验组件资产和 SHA256,再在真实 KVM 环境中运行
owner E2E 与 platform 组合用例.发布包记录精确版本组合,避免混用不同日期或不同聚合
版本的资产.

这些机制支持生产部署中的版本固定,故障定位和可重复验证.部署方仍需为 TLS,持久化
后端,备份,监控,网络策略和容量配置生产级实现.

## 9. 性能与容量

系统总览不把单次测试数字或容量推演写成普遍能力.性能结果统一记录在
[perf.md](perf.md),并按组件拥有的 E2E/perf 入口回归.

可对外比较的结果至少应同时说明:

- 聚合版本和全部相关组件版本或精确 commit;
- CPU,内存,磁盘,网络,KVM 和宿主软件环境;
- 本地文件,共享文件或对象存储路径;
- cache 层级以及 cold/hot 状态;
- 沙箱 vCPU,内存,磁盘和 workload;
- 样本数,聚合方法,分位数和失败率;
- 从哪个事件到哪个事件的延迟或吞吐口径;
- 原始报告或可复现测试入口.

缺少这些上下文的数字不能用于承诺节点容量,启动/恢复延迟,缓存命中率或存储收益.
设计目标必须明确标注为 target,只有在指定版本和环境完成验证后才成为该测试范围内的
结果.

## 10. 部署与发行状态

Kuasar Sandbox 具备单节点和集群拓扑,节点资源准入与恢复,独立进程和故障域,组件
独立版本和聚合版本,跨组件真实 MicroVM E2E,Release 资产校验和精确版本组合.

当前公开聚合发行通道为 Preview,尚未发布 Stable 聚合版本.当前 GitHub Release 提供
Linux x86_64 预构建资产;源码构建支持 x86_64 和 aarch64,但源码可构建架构不能自动视为
已发布资产架构.最新状态以
[GitHub Releases](https://github.com/kuasar-sandbox/kuasar-sandbox/releases) 和
[release.md](release.md) 为准.

## 11. See Also

- [deployment.md](deployment.md) - 部署拓扑,进程,端口和启停依赖
- [perf.md](perf.md) - 带环境口径的组件性能基线与回归方法
- [release.md](release.md) - 组件/聚合版本,资产和发布事务
- [Demo](../test/demo/DEMO.md) - 本地体验环境与 E2B SDK 演示
- [Full validation](../test/QUICKSTART.md) - Aggregate Release 完整验证入口
- [`orchestrator`](https://github.com/kuasar-sandbox/orchestrator/tree/main/docs) - node,资源和 cluster 设计
- [`sandboxer`](https://github.com/kuasar-sandbox/sandboxer/tree/main/docs) - MicroVM,snapshot 和 Guest 协同
- [`accelerator`](https://github.com/kuasar-sandbox/accelerator/tree/main/docs) - Manifest,store 和 cache
- [`connector`](https://github.com/kuasar-sandbox/connector/blob/main/docs/vswitch.md) - vSwitch 实现与网络细节
- [`guest-runtime`](https://github.com/kuasar-sandbox/guest-runtime/tree/main/docs) - runtime,vmlinux 和 flatten
