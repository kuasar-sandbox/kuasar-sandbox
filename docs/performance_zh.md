[English](performance.md) | [简体中文](performance_zh.md)

# 性能测量、容量与调优

本篇定义可比较的测量、容量验收和调优方法。历史结果保留其环境和版本，不作为所有部署的性能承诺。

本文定义 Kuasar Sandbox 性能测试的口径,入口,证据要求和回归方法.性能结果只对
记录的版本,硬件,数据路径,cache 状态,沙箱规格和 workload 有效,不能从一次开发
环境测试外推为所有生产部署的能力.

早期文档中的历史绝对值没有同时保存精确聚合/组件版本,完整硬件,失败率和原始报告
位置,因此不再作为公开基线保留.当前文档不发布无证据链的启动延迟,恢复延迟,cache
命中率,单节点容量或存储收益数字.

## 测量入口

主仓聚合入口:

```bash
make perf E2E_WORKDIR="$PREPARED"
make perf-sandbox E2E_WORKDIR="$PREPARED"
make perf-sandbox-manifest
make perf-sandbox-working-set
make perf-density
make perf-warmpool-dedup
make test-uffd-performance-gate
make test-perf-tools
```

`make perf` 先构建当前兄弟仓源码,再运行 accelerator cache benchmark 和主仓的 sandbox,
Manifest、working-set、density 与 warm-pool deduplication harness。density harness 存在[节点资源与密度](#节点资源与密度)所述的兼容性限制；聚合入口不代表每个 harness 都已通过当前产品验证。单独运行 cache benchmark:

```bash
make -C ../accelerator perf-cache
```

真实 MicroVM 路径需要可读写的 `/dev/kvm`,root 或无交互 sudo,所需镜像与运行制品.
Manifest 和 density harness 还会按脚本检查 Docker,网络与文件系统工具.缺少这些条件时
产生的 skip 不是性能验证成功;发布证据必须记录实际退出状态和所有失败样本.

## 结果证据合同

任何准备写入文档,Release Notes 或容量规划的结果,至少同时保存:

| 维度 | 必需证据 |
|---|---|
| Version | 聚合 tag/commit,以及 accelerator,connector,sandboxer,orchestrator,runtime,vmlinux 的精确 tag/commit |
| Host | CPU 型号与拓扑,内存,磁盘,网络,Linux,KVM/VMM,裸金属或嵌套虚拟化 |
| Data path | local file,named shared file 或 Manifest;FS/S3-compatible backend;cache 层级和 cold/hot 状态 |
| Sandbox | vCPU,memory zone,资源声明,磁盘,kernel/runtime,template 或 snapshot 来源 |
| Workload | 镜像 digest,启动/恢复步骤,输入数据,并发,持续时间和随机种子 |
| Statistics | 样本总数,成功/失败/skip 数,失败率,聚合方法以及报告的分位数 |
| Timing | 起止事件,是否包含下载/构建/导入,时钟来源和 timeout |
| Source | harness 命令,环境变量,原始日志,machine-readable samples 和 CI Run URL |

`sandbox-perf.sh` 通过公共 runner 执行当前 `sandbox.lifecycle.sh` 和
`image.manifest-boot.sh`，要求 `PREPARED` 已为这些用例完成准备。源码 harness 不随
platform 发布，Makefile 直接从源码路径调用，不重新构建预备产品。Case wallclock
包含用例设置、断言及清理，不是 `Sandbox.create()` 到就绪，也不与历史 T0→exit
数值直接比较。原始 stats、runner result 和失败日志全部保留；可变用例状态使用独立短目录
`$TMPDIR/kp-*`（默认 `/var/tmp`，workbench 内为 `/build/t`），避免 Unix socket
路径过长，其位置记录在报告旁；首个失败会非零退出。
`make perf` 的其他 harness 仍使用其源码构建输入；比较时必须记录各自来源，不能
假定它们自动与 `PREPARED` 相同。`sandbox-perf-manifest.sh` 仍只聚合成功样本，引用
其结果须同时保留请求次数、完整日志及成功/失败/skip 数，不能只依据成功样本数。

结果缺少任一必要维度时,可以用于本地诊断,但不能作为项目级性能事实.设计阈值必须标注
为 target 或 regression gate;门禁通过只说明该候选满足该测试合同,不自动形成生产 SLO.

## Cache 与数据路径

### 测量对象

accelerator cache benchmark 默认为 `BENCH_SCENARIO=local`。测量 tiered cache 或
shard fan-out 时须显式选择 `tiered-l1` 或 `tiered-shard-l2`。这些由脚本管理的场景以
另一个 local cache daemon 充当 upstream origin，不是真实的 Store/FS/S3 backend。
Store 回源性能需要单独配置并记录真实拓扑。可比较的 cache/数据路径报告至少区分:

- value size,prefill 数量,并发和运行时长;
- client/server CPU 绑定与网络传输方式;
- L1/L2 cold,warm 和部分命中状态;
- FS 或 S3-compatible origin;
- Get/Put 的 p50,p95,p99,吞吐,错误和回源比例.

`CACHE_CTL_TIMING=1` 可以提供 cache 内部分段诊断;pprof 和 trace 用于定位 CPU,分配,
网络与调度开销.这些内部计时不能替代从客户端观察的端到端口径.

### 路径解释

三种工件路径必须分开比较:

| 路径 | 跨节点条件 | Cache 条件 | 恢复依赖 |
|---|---|---|---|
| Local file | 节点亲和或部署方复制 | 依赖文件系统 page cache | 需要原节点文件仍存在 |
| Named shared file | NAS/NFS/共享文件系统对目标节点可见 | 依赖共享文件系统和 host page cache | 不要求转换为 Manifest |
| Manifest | FS/S3-compatible store 对目标节点可达 | 可直接回源,也可使用 local/tiered cache | 由完整 Manifest 父链定位内容 |

不能把普通 `file://` 统一解释为"不支持共享".当命名 location 位于共享文件系统时,
它可以跨节点访问.也不能把 Manifest 的一次 cold-cache 成本写成所有后续请求的固定成本.

`make perf-warmpool-dedup` 对同一镜像独立启动后的 overlay 和 memory snapshot 比较非零 chunk hash 集合的重合度；单次 image ingest 的共享只作为基本正确性检查。该受控负载不衡量通用跨镜像去重。

Manifest 配置 facade 在 `cache.endpoint` 非空时选择 cache client，不会同时设置 cache miss 或故障时的直接回源路径。只有 `cache.endpoint` 为空时才选择直接读取 store。应单独记录 cache server 配置的回源行为，不能假设 local-cache 模式会自动获取缺失数据。

内容复用结果只能解释为该数据集,安全域和父层关系下的观测.同模板实例的首要共享来自
明确只读父层;不同虚机运行后偶然相同的内存字节不是快照效率前提.

## Sandbox 启动,快照与恢复

### 基础矩阵

`test/perf/sandbox-perf.sh` 表征当前 file/Manifest 生命周期用例（计时范围见上文）,
`test/perf/sandbox-perf-manifest.sh` 展开 Manifest cold/hot cache,snapshot publish 和
restore 场景.报告应至少包含:

- 从启动请求到 Guest 应用 ready 的 wallclock;
- sandboxer 内部阶段,VMM 和 Guest ready 证据;
- 按需读取的请求,字节,错误和 cache 来源;
- snapshot publish,父层解析和 restore 的独立时长;
- 请求迭代数,成功样本,失败/skip 计数与失败率,以及成功样本的分位数.

`<run-root>/<sid>/ctl.sock` 只证明 host 控制 socket 存在.性能 harness 必须使用实际
Guest 命令,健康检查或 workload ready 条件作为完成信号.

### Working-set 矩阵

`test/perf/sandbox-perf-working-set.sh` 对同一个不可变父层执行配对的 capture,publish,
cold restore 和可选 prefetch 比较.它输出:

```text
environment.json
samples.jsonl
local-crypto-samples.jsonl
report.md
raw/
```

`environment.json` 记录 revisions,host,image,binaries 和 workload;`samples.jsonl`
保留逐样本事实;`raw/` 保存 snapshot,publisher,restore 和 cache 证据.对外引用必须保留
完整目录并给出对应 CI Run URL,不能只摘录 `report.md` 的某个分位数.

源码候选 CI 仅在 x86_64 且所选 owner 包含 `platform` 或 `sandboxer` 时选择 `working-set-smoke`，并非每种架构或 exact-assets run 都会运行。选中的步骤设置 `PERF_ITERS=1`，用于发现明显回归。正式性能报告应显式设置样本数,
保存所有样本,并将 smoke 与统计报告分开命名.

### 快照语义

测量应分别覆盖:

- 从同一模板父层创建多个独立实例的 1:N 场景;
- 暂停并恢复同一稳定 Sandbox ID 的 1:1 场景;
- 本地恢复,命名共享文件恢复和 Manifest 远程恢复;
- memory/disk 父链完整时的成功路径,以及父层缺失或校验失败的错误路径;
- cold cache,warm cache和显式 prefetch.

快照层级,数据载体和 cache 状态是三个独立变量,不能用其中一个结果替代另外两个.

## 节点资源与密度

### Workload 模型

`test/perf/workload.py` 提供 `idle`,确定性的 grow/rest cycles 和重尾 active/idle
三类负载.运行 `test/perf/density-perf.sh` 时应显式记录并发,memory zone,resource
floor/startup/capacity,workload 参数,观察窗口和随机种子.

当前 density harness 不能原样兼容当前 conductor 配置：它仍输出已移除的 `proxy.mode`、`resource_listen.audit_path` 和 `resource_listen.dampening` 字段。默认 `$WORK/run` 也需要满足当前最大 ID 的 Unix socket 路径预算。在 harness 更新并针对精确组件版本验证之前，不得把 `make perf-density` 当作当前 conductor 的验收。

脚本的文本报告采样 host `MemAvailable`、从日志提取的 controller 计数以及 cgroup `memory.events.local`（`high` 和 `oom`，不是 `oom_kill`）。它不能证明 Guest 业务完成，也不收集下列完整的各池和绑定证据。其打印的 `PASS` 和容量推算不是生产容量结论；最终打印的 verdict 本身也不会转换为失败退出状态。除报告外还须保留 stderr 和工作日志（`PERF_KEEP=1` 保留工作目录）。

合格的密度报告必须同时观察:

- 每个沙箱的 admission,settled,grant,reject 和终态;
- host `MemAvailable` 与 sandbox cgroup `memory.current/events.local`;
- Guest workload 是否完成,以及 Guest/cgroup OOM;
- 启动,活跃,回收和终止阶段的时间线;
- 各资源池的调度 capacity、运行时 allocatable、水位、startup reserve、增长速率及恢复义务；
- Runner/Builder pool 的映射、空闲 `size`、槽位 `capacity`、`priority`、NUMA/CPU 绑定与实际掩码。同时比较各池压力和节点总量；其他池有空闲容量，不证明所选池能准入该请求。

### 解释边界

密度由 workload 峰值 working set,活跃比例,VMM/Guest 常驻开销,回收时延,暂停策略
和节点安全余量共同决定.memory zone 是上限,不是可直接换算为物理占用或实例数的常数.

容量规划除应用 RSS 外,还必须包含 guest 内核、页表、slab 和 guest 服务占用,并按
workload 实测预留余量.guest 内 OOM 与 host cgroup OOM 是不同事件:host
`memory.events` 的 OOM 计数为零不能证明 guest 应用存活.还必须记录应用完成、退出及
guest 侧证据.退出码 137 表示 SIGKILL 类退出,单凭此值不能判定其原因.本文不设定
普遍 guest 开销常数或固定 capacity 倍率.

资源职责必须分开解释:

- sandboxer 执行单沙箱 Balloon,Cgroup,VMM 以及 pause/resume 协同;
- node-ctl Reservation Controller 执行 admission、各资源池预算、水位、Grant、Inventory
  和恢复,不直接接管单沙箱闭环.

Balloon 不是唯一弹性来源.空闲 CPU 调度,非活跃内存回收,Cgroup 限制,节点准入以及
长等待实例的 pause 都影响有效利用率.任何容量结论都必须在声明资源和准入模型范围内
报告 OOM 与有效工作丢失,不能把 OOM 当作超卖成功.

高密度是资源利用率提高后的结果,不是预先指定的实例数量承诺.

### Agent 业务完成基准

原有 `density-perf.sh` 旨在表征资源控制行为，受上述兼容性和证据限制，不等同于完整 Agent 业务吞吐基准。`test/perf/agent-density/benchmark.py` 检查 BMS coding-Agent fixture 的 `summary.json` 和 `process-exit.json`：逻辑任务总数须为 24 或 48，计划须按固定三并发与动态十六并发分批顺序执行（末批可以不足）；全部任务被接纳并正确完成；工具工作次数与确定性结果哈希正确；没有宿主机 OOM Kill、清理失败或进程异常退出。每批从零重新编号的是**任务 ID，而不是重新执行业务**：比较按计划批次顺序、批内任务 ID 展平后的逻辑任务。准入拒绝不能算业务完成。运行实例数采样峰值单独报告，不能据此认定分配器的配置上限。

校验器要求计划禁用业务重试，且每个任务观测到一次业务启动、任务与启动身份一致；报告 `planned_business_retries` 和 `observed_business_restarts`，但**不独立审计全部客户端 POST 尝试**。非 200 HTTP 轮询错误、传输错误及迟到响应分别计数，并与逐请求记录和逐任务计数交叉核对。一次轮询失败不自动等于业务失败或业务重试；HTTP 200 响应身份校验失败、最终结果错误或业务未完成仍须判失败。独立审计需保留每批 `timeline.jsonl`、原始请求与进程日志、精确环境和版本。校验器不负责启动沙箱，也不认证这些外部记录；报告 `seconds` 和 `speedup` 使用各批预热与执行时间之和，外层总耗时另含准备和清理。

2026-10-08 BMS 本地**匹配实验**采用 8C16G、12GiB 资源池、1C4G 沙箱、allocatable 256MiB、startup 512MiB、无 swap。合成 coding-Agent 负载采用 `held` profile、192MiB 峰值参数、每任务四轮工具工作。48 Agent 均 48/48 正确完成：x86_64 固定三并发 **851 秒**、动态十六并发 **380 秒**；ARM64 分别为 **802 秒、333 秒**，取外层 case START/DONE 日志，包含准备和清理。对应校验器的预热加执行时间为 832.21/350.49 秒、784.44/302.53 秒。计划禁用业务重试、观测启动次数均为一，宿主机 OOM 和清理校验通过。x86 动态组 **735 次轮询中 734 次成功、1 次 HTTP 错误**，最终业务仍正确完成，不能表述为零转发失败。这些结果只适用于记录的负载与版本，不代表与另一套静态内存管理器的比较，也不是生产环境通用容量承诺。24/48 Agent 证据表和审计边界见 `test/perf/agent-density/README.md`。

## Cluster 控制面

cluster 性能必须区分热路径和冷路径:

```text
hot path:
client ──► router READY route cache ──► node/proxy/envd

cold path:
router ──► registry route_link ──► node/placer
node   ──► registry node_link/node_list
placer ──► group import and placement
```

热路径验证 route cache 命中后不进入 Resolve/Reserve.冷路径分别测量 create,connect,
exec-session,data activation,node 状态变化,group import 和成员切换.每个结果必须记录
registry 副本/owner 配置,sandbox-group 数量,route cache 状态,node 数量,请求完成条件
和失败率.

placer 只做 group 导入和放置建议,最终资源确认在 node admission.测量不能把 placer
吞吐解释为已创建 MicroVM 的吞吐.

## Release 与 CI 证据

源码候选 CI 在公共 prepare 之前构建精确产品与 helper revision。产品 E2E 通过 `test/e2e/e2e` 运行扁平用例；源码/unit/race/vet 和 UFFD 性能保持独立，按上述选择规则运行的 working-set smoke 有单独必需 job 和结果。exact-assets 验证消费包中的产品、helper 和 runner，不重新构建它们。架构结果记录每个已选用例、明确排除项及预备输入身份。

工作流与证据入口:

- [`.github/workflows/integration-tests.yml`](../.github/workflows/integration-tests.yml):源码候选的
  端到端集成测试与发行资产验证;
- [`test/perf/`](../test/perf):主仓性能 harness 与报告生成器;
- [`test/e2e/e2e`](../test/e2e/e2e):公共预构建产品用例 runner。

绿色聚合状态本身不是某个性能结论的证据.引用结果时必须给出 Run URL,base/head SHA,
运行模式,相关 job log 和下载后的原始 artifact.如果 workflow 只运行 smoke,必须明确写
`smoke`,不能改称完整统计验证.

## 回归检查

修改 cache 路径时:

```bash
GOWORK=off make -C ../accelerator test
make -C ../accelerator perf-cache
```

修改 sandbox snapshot/restore/按需读取路径时:

```bash
GOWORK=off make -C ../sandboxer test
make perf-sandbox E2E_WORKDIR="$PREPARED"
make perf-sandbox-manifest
make perf-sandbox-working-set
make test-uffd-performance-gate
```

修改节点资源控制路径时，使用下列单元测试入口。density 入口仅适用于已经验证兼容的 harness 版本，限制见上文：

```bash
GOWORK=off make -C ../orchestrator test
make perf-density
```

修改报告脚本或门禁解析时:

```bash
make test-perf-tools
```

每次对比使用相同 harness 参数和环境,同时查看成功样本,失败/skip,原始日志与
machine-readable 输出.超过已批准 gate 的候选必须先定位根因;没有完整证据时不更新
公开基线.

<a id="capacity-example"></a>
## 带假设预算的容量验收

这是**假设算例，不是实测性能**。假设 16-vCPU/32-GiB 节点运行执行 Python、读取
代码仓、写小型工作区并请求外部服务的 agent，每个活跃实例常态 1 GiB、同步突发
2.5 GiB、验收负载下 2 CPU。先用实际镜像测量，OCI 镜像大小不是 RSS。

初始预留可为 host 4 GiB、全体 control/VMM/非工作负载开销 4 GiB、完整 accelerator/
cache 路径 6 GiB、额外一次冷恢复瞬时开销 3 GiB。余量
`32 - 4 - 4 - 6 - 3 = 15 GiB`，可容纳六个假设 2.5-GiB 突发实例。若工作负载
CPU 预算为 12，`12 / 2 = 6` 也得到六个初始准入。常态六实例仅 6 GiB，不代表
所有实例可同时活跃时可以准入十五个。Guest capacity、allocatable/startup、
controller grant 和实际计费使用量不同；按 [Node 资源契约](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-resource_zh.md)
配置，不把算术当 YAML 默认值。

6-GiB cache 预算必须覆盖 Go heap、native RocksDB block/blob cache/memtable、
inflight 请求/blob/解密 buffer、compaction 和文件 page-cache 压力。
`GOMEMLIMIT` 或 `disk_bytes * mem_ratio` 均不是进程 RSS 上限。按
[Cache 预算契约](https://github.com/kuasar-sandbox/accelerator/blob/main/docs/cache_zh.md)
记录 cgroup/RSS 峰值、占用、compaction、磁盘/网络。大型参考 YAML 展示选项，
不是首个生产部署的推荐规模。

| 测量 | 执行和记录 | 验收/调整 |
|---|---|---|
| 常态 | 代表性请求按 1、3、6 并发，热稳态 | 应用延迟/错误、内存、CPU、存储增长和吞吐符合预先声明 SLO。 |
| 同步突发 | 六实例同时进入峰值阶段，包括写入 | 无 OOM/数据丢失，准入及余量有效；超过假设 2.5 GiB 则降低准入或加预算再测。 |
| 冷恢复 | 常态业务继续时从冷测试 cache/目标节点恢复已合格负载 | 测 restore-to-ready、首次使用 p95/p99、源尝试、带宽、总内存峰值，包含恢复瞬时量；不得清生产 cache。 |
| 持续运行 | 重复 pause/resume/清理，覆盖 compaction/保留作业 | RSS、磁盘、FD、inflight 不无限增长，状态断言与清理均成功。 |

测试前设定数值化应用/恢复 SLO，这里不作通用延迟承诺。完整矩阵通过后每次只改
一个预算/并发变量。完整性/身份失败或 OOM 时停止，保留证据，kill 自有测试并回到
上次合格限制。冷/热结果分开报告，包含未运行探针。

## See Also

- [系统架构](architecture_zh.md) - 系统语义,组件边界与性能证据要求
- [`deployment_zh.md`](deployment_zh.md) - 数据后端,进程拓扑和部署选择
- [accelerator Cache](https://github.com/kuasar-sandbox/accelerator/blob/main/docs/cache_zh.md) - cache 架构和组件 benchmark
- [sandboxer 生命周期](https://github.com/kuasar-sandbox/sandboxer/blob/main/docs/runtime_zh.md) - snapshot/restore 和统计字段
- [节点资源](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-resource_zh.md) - 节点资源控制协议
- [集群设计](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/cluster_zh.md) - registry/router/placer 设计
