# perf - 性能验证与调优

本文定义 Kuasar Sandbox 性能测试的口径,入口,证据要求和回归方法.性能结果只对
记录的版本,硬件,数据路径,cache 状态,沙箱规格和 workload 有效,不能从一次开发
环境测试外推为所有生产部署的能力.

早期文档中的历史绝对值没有同时保存精确聚合/组件版本,完整硬件,失败率和原始报告
位置,因此不再作为公开基线保留.当前文档不发布无证据链的启动延迟,恢复延迟,cache
命中率,单节点容量或存储收益数字.

## 1. 测量入口

主仓聚合入口:

```bash
make perf
make perf-sandbox
make perf-sandbox-manifest
make perf-sandbox-working-set
make perf-density
make test-uffd-performance-gate
make test-perf-tools
```

`make perf` 先构建当前兄弟仓源码,再运行 accelerator cache benchmark 和主仓的 sandbox,
Manifest,working-set 与 density harness.单独运行 cache benchmark:

```bash
make -C ../accelerator perf-cache
```

真实 MicroVM 路径需要可读写的 `/dev/kvm`,root 或无交互 sudo,所需镜像与运行制品.
Manifest 和 density harness 还会按脚本检查 Docker,网络与文件系统工具.缺少这些条件时
产生的 skip 不是性能验证成功;发布证据必须记录实际退出状态和所有失败样本.

## 2. 结果证据合同

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

当前 `sandbox-perf.sh` 和 `sandbox-perf-manifest.sh` 的聚合输出只保留成功迭代并报告
成功样本数 `N`;失败迭代只出现在完整运行日志中.使用这些入口时必须同时保存请求的
`ITERS`,完整日志以及成功/失败/skip 计数.无法恢复总分母和失败率的聚合输出不能单独
作为性能证据.

结果缺少任一必要维度时,可以用于本地诊断,但不能作为项目级性能事实.设计阈值必须标注
为 target 或 regression gate;门禁通过只说明该候选满足该测试合同,不自动形成生产 SLO.

## 3. Cache 与数据路径

### 3.1 测量对象

accelerator cache benchmark 分别观察 local cache,tiered cache,shard fan-out 和 store
origin.至少区分:

- value size,prefill 数量,并发和运行时长;
- client/server CPU 绑定与网络传输方式;
- L1/L2 cold,warm 和部分命中状态;
- FS 或 S3-compatible origin;
- Get/Put 的 p50,p95,p99,吞吐,错误和回源比例.

`CACHE_CTL_TIMING=1` 可以提供 cache 内部分段诊断;pprof 和 trace 用于定位 CPU,分配,
网络与调度开销.这些内部计时不能替代从客户端观察的端到端口径.

### 3.2 路径解释

三种工件路径必须分开比较:

| 路径 | 跨节点条件 | Cache 条件 | 恢复依赖 |
|---|---|---|---|
| Local file | 节点亲和或部署方复制 | 依赖文件系统 page cache | 需要原节点文件仍存在 |
| Named shared file | NAS/NFS/共享文件系统对目标节点可见 | 依赖共享文件系统和 host page cache | 不要求转换为 Manifest |
| Manifest | FS/S3-compatible store 对目标节点可达 | 可直接回源,也可使用 local/tiered cache | 由完整 Manifest 父链定位内容 |

不能把普通 `file://` 统一解释为"不支持共享".当命名 location 位于共享文件系统时,
它可以跨节点访问.也不能把 Manifest 的一次 cold-cache 成本写成所有后续请求的固定成本.

内容复用结果只能解释为该数据集,安全域和父层关系下的观测.同模板实例的首要共享来自
明确只读父层;不同虚机运行后偶然相同的内存字节不是快照效率前提.

## 4. Sandbox 启动,快照与恢复

### 4.1 基础矩阵

`test/perf/sandbox-perf.sh` 比较 file 和 Manifest cold-start 路径,
`test/perf/sandbox-perf-manifest.sh` 展开 Manifest cold/hot cache,snapshot publish 和
restore 场景.报告应至少包含:

- 从启动请求到 Guest 应用 ready 的 wallclock;
- sandboxer 内部阶段,VMM 和 Guest ready 证据;
- 按需读取的请求,字节,错误和 cache 来源;
- snapshot publish,父层解析和 restore 的独立时长;
- 请求迭代数,成功样本,失败/skip 计数与失败率,以及成功样本的分位数.

`<run-root>/<sid>/ctl.sock` 只证明 host 控制 socket 存在.性能 harness 必须使用实际
Guest 命令,健康检查或 workload ready 条件作为完成信号.

### 4.2 Working-set 矩阵

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

主线 BMS 的 working-set 项是 smoke,用于发现明显回归.正式性能报告应显式设置样本数,
保存所有样本,并将 smoke 与统计报告分开命名.

### 4.3 快照语义

测量应分别覆盖:

- 从同一模板父层创建多个独立实例的 1:N 场景;
- 暂停并恢复同一稳定 Sandbox ID 的 1:1 场景;
- 本地恢复,命名共享文件恢复和 Manifest 远程恢复;
- memory/disk 父链完整时的成功路径,以及父层缺失或校验失败的错误路径;
- cold cache,warm cache和显式 prefetch.

快照层级,数据载体和 cache 状态是三个独立变量,不能用其中一个结果替代另外两个.

## 5. 节点资源与密度

### 5.1 Workload 模型

`test/perf/workload.py` 提供 `idle`,确定性的 grow/rest cycles 和重尾 active/idle
三类负载.运行 `test/perf/density-perf.sh` 时应显式记录并发,memory zone,resource
floor/startup/capacity,workload 参数,观察窗口和随机种子.

报告同时观察:

- 每个沙箱的 admission,settled,grant,reject 和终态;
- host `MemAvailable` 与 sandbox cgroup `memory.current/events.local`;
- Guest workload 是否完成,以及 Guest/cgroup OOM;
- 启动,活跃,回收和终止阶段的时间线;
- Reservation pool 的水位,startup reserve 和 operational margin.

### 5.2 解释边界

密度由 workload 峰值 working set,活跃比例,VMM/Guest 常驻开销,回收时延,暂停策略
和节点安全余量共同决定.memory zone 是上限,不是可直接换算为物理占用或实例数的常数.

资源职责必须分开解释:

- sandboxer 执行单沙箱 Balloon,Cgroup,VMM 以及 pause/resume 协同;
- node-ctl Reservation Controller 执行 admission,共享池,水位,Grant,Inventory
  和恢复,不直接接管单沙箱闭环.

Balloon 不是唯一弹性来源.空闲 CPU 调度,非活跃内存回收,Cgroup 限制,节点准入以及
长等待实例的 pause 都影响有效利用率.任何容量结论都必须在声明资源和准入模型范围内
报告 OOM 与有效工作丢失,不能把 OOM 当作超卖成功.

高密度是资源利用率提高后的结果,不是预先指定的实例数量承诺.

### 5.3 Capacity sizing(实测校准)

zone/capacity 只约束应用峰值是不够的:guest 内核,page table,slab 与
`sandbox-init` 另有 ~50 MiB 的常驻开销.当 zone 只按应用 P99 × 1.2 规划时,
突发峰值期间 guest 内核 direct reclaim 会先于 balloon deflate 路径触发
guest 内 OOM(应用被 SIGKILL,exit code 137).该失败**不会体现在 cgroup
`memory.events` 中**(host cgroup OOM 计数为 0),只有检查 guest 应用退出码
才能发现:实测在 herded cycles 负载下 35–53% 的沙箱应用被静默杀灭.

实测校准后的容量公式:

```text
capacity = (App_P99_RSS × 1.2) + Guest_OS_Overhead(≈ 50 MiB)
```

 zone 为 lazy memfd,未被 fault 的部分不消耗 host 物理页,因此按此公式放大
zone 不降低密度;实测相同负载下 guest OOM 从 35–53% 降为 0,而每沙箱
host 占用不变.标准剖面(实测校准):

| 剖面 | 应用 RSS 区间 | zone/capacity | floor |
|---|---|---|---|
| light agent | 16–64 MiB | 192 MiB | 64 MiB |
| interactive tool agent | 64–128 MiB | 320 MiB | 64 MiB |
| heavy SWE agent | 128–256 MiB | 512 MiB | 64 MiB |

配套要求:密度结论必须统计 guest 应用非零退出(exit 137 等),不能只看
host cgroup OOM 计数;`deflate_on_oom` 在 zone 紧张时可能输给 guest OOM
killer 的竞速,上述余量就是为该竞速窗口预留的.

## 6. Cluster 控制面

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

## 7. Release 与 CI 证据

源码候选 BMS 会构建精确 revision,运行 owner E2E,UFFD regression gate 和 working-set
smoke,并上传 `ci-metadata-<run-id>-<attempt>` artifact.聚合 Release 的 exact-assets 模式
从同一个聚合包解压所有资产,在真实 KVM 上运行完整 `test/e2e/run_all.sh`.

工作流与证据入口:

- [`.github/workflows/bms-e2e.yml`](../.github/workflows/bms-e2e.yml):源码候选与
  exact-assets BMS;
- [`test/perf/`](../test/perf/):主仓性能 harness 与报告生成器;
- [`test/e2e/run_all.sh`](../test/e2e/run_all.sh):聚合预构建 owner + platform E2E.

绿色聚合状态本身不是某个性能结论的证据.引用结果时必须给出 Run URL,base/head SHA,
运行模式,相关 job log 和下载后的原始 artifact.如果 workflow 只运行 smoke,必须明确写
`smoke`,不能改称完整统计验证.

## 8. 回归检查

修改 cache 路径时:

```bash
GOWORK=off make -C ../accelerator test
make -C ../accelerator perf-cache
```

修改 sandbox snapshot/restore/按需读取路径时:

```bash
GOWORK=off make -C ../sandboxer test
make perf-sandbox
make perf-sandbox-manifest
make perf-sandbox-working-set
make test-uffd-performance-gate
```

修改节点资源控制路径时:

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

## 9. See Also

- [`kuasar-sandbox.md`](kuasar-sandbox.md) - 系统语义,组件边界与性能证据要求
- [`deployment.md`](deployment.md) - 数据后端,进程拓扑和部署选择
- `accelerator/docs/cache.md`(发布包:`docs/cache.md`) - cache 架构和组件 benchmark
- `sandboxer/docs/sandbox.md`(发布包:`docs/sandbox.md`) - snapshot/restore 和统计字段
- `orchestrator/docs/node-resource.md`(发布包:`docs/node-resource.md`) - 节点资源控制协议
- `orchestrator/docs/cluster.md`(发布包:`docs/cluster.md`) - registry/router/placer 设计
