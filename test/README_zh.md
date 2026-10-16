[English](README.md) | [简体中文](README_zh.md)

# 测试组织

首次体验使用[快速开始](../docs/quickstart_zh.md)；发布用例的推荐环境和完整命令见
[发布验证](QUICKSTART_zh.md)。Workbench 只提供环境，仍调用同一个公共 runner。
下面的直接 runner 命令是原生执行参考，不要求用户另建一套宿主依赖环境。

产品 E2E 遵循唯一契约：**预构建制品 → prepare → `<suite>.<case>.sh` → 公共 runner**。完整文件名就是用例 ID，第一段就是 suite。九个 suite 为 `basic`、`storage`、`image`、`network`、`sandbox`、`snapshot`、`orchestrator`、`builder`、`telemetry`。

组件在自己的 `test/e2e/cases/` 和 `test/e2e/lib/` 维护用例及底层 helper。主仓维护 `test/e2e/platform/cases/basic.demo.sh`。源码模式组装从已准入并固定的候选与基线 Tag 输入复制用例到扁平的 `test/e2e/cases/`，helper 放入 `test/e2e/lib/<owner>/`。重复 ID、未知 suite 和 owner runner 都会被拒绝。维护归属不影响公共选择语义。

platform 包携带公共 `test/e2e/e2e` runner 和两个架构的预构建 helper。更早的 source/release build 按 `test/demo/requirements.lock` 获取完整 Demo SDK wheel 依赖闭包，并打包两种架构的 wheelhouse。prepare 解析镜像，仅从本地 hash-locked wheel 安装 SDK（`--no-index --find-links`、`--require-hashes`），记录包名/版本/wheel 身份、安装文件摘要、权限和镜像内容 ID。wheel 缺失或变化时失败。执行只消费不可变工作区，不构建产品或 helper、不发现兄弟源码树、不拉取替代镜像、不使用宿主机 helper。被测 Build、flatten、snapshot 和发布操作仍真实执行。

编译型 helper 的完整清单和构建配方由各 owner 的 `release/helper-inputs.json` 维护。构建阶段从固定源码集校验声明、调用 owner 配方，并拒绝缺失或额外输出。公共 prepare 消费打包契约和二进制；用例通过声明的环境变量别名或 `E2E_HELPERS_DIR/<name>` 使用 helper。新增 owner helper 无需修改平台文件名清单。归档原始权限保留为证据；安全的私有解包可收紧权限，prepare 记录副本的实际权限。

发布包包含规范用例、递归发现的 owner 运行库与实际 Demo 输入。源码单元测试和性能工具保留在源码树中。所选双语用户指南位于 `guide/`，accelerator 与 guest-runtime 的 E2E README 双语文件仍位于 `test/e2e/<owner>/`。轻量宿主机启动器及说明位于 `workbench/`。helper 与 wheelhouse 保留原有契约。

```bash
python3 /release/test/e2e/e2e list --suite storage
python3 /release/test/e2e/e2e prepare --release-dir /release --workdir /tmp/kuasar-prepared --suite storage --exclude storage.obs.sh
sudo python3 /tmp/kuasar-prepared/test/e2e/e2e run --workdir /tmp/kuasar-prepared --suite storage --exclude storage.obs.sh
```

`--suite` 与 `--include` 取并集，`--exclude` 按完整文件名排除。未知选择和空集会失败。`--all` 包含需要凭据的 `storage.obs.sh`，公共 CI 明确排除该用例。没有 owner、tag、capability 或 fixture graph 选择器。Makefile 包装入口要求 `RELEASE_DIR`、全新的 `E2E_WORKDIR`，可选 `E2E_ARGS`。

每次 `run` 调用都为适用的 helper 提供新的私有目录，只在本次调用内复用不可变模板
准备。即使显式指定 `--run-root`，目录也位于准备输入和用例自有工作目录之外。用例
收到 `E2E_TEMPLATE_CACHE_DIR` 和完整准备 provenance 摘要
`E2E_TEMPLATE_CACHE_PROVENANCE`，环境中原有的同名变量会被清除。runner 在
`result.json` 中记录目录，并随运行状态保留供检查，但后续运行不会选用它。
`run --no-template-cache` 清除两个变量且不创建缓存，便于与普通准备对比。旧 helper
可以忽略这些变量，行为不变。

消费端须将内容绑定到准备身份、原生架构、完整镜像 config ID 和版本化转换配方，
验证条目并持锁原子发布。匹配条目损坏即失败，不静默重建。只允许共享不可变且未加密
的镜像内容；每个用例仍独立拥有密钥、Store、template/Build 身份、可写磁盘和真实
Guest 生命周期，不共享活动 VM、快照或可变数据库。专门的 Builder 和 Demo 用例
保留真实构建。runner 合同检查本身不能验证消费端缓存命中或证明性能收益。

prepare 接受 `--deps-dir` / `E2E_DEPS_DIR` 指定已验证的本地镜像归档，以及 `--offline` / `E2E_OFFLINE=1` 禁止下载依赖。未配置时保持在线行为。本地匹配项验证时不向远端检查新鲜度，无效匹配项也不触发远端修复。外部请求来自单一用例/架构函数，构建期输入收集也可调用该函数。优先级、身份证据与失败行为见[本地及离线准备](QUICKSTART_zh.md#本地及离线准备)。

原生 x86_64 和 aarch64 使用相同的公共 `--all --exclude storage.obs.sh` 选择。
prepare 要求实际宿主、产品、helper 和镜像架构一致，包括两种 orchestrator fixture。
在昂贵生成步骤前校验所选 helper；`sandbox.cgroup.sh` 要求该架构 `helpers.json`
中的精确源码静态无 libc probe。未选择的 helper 不成为前置条件。
Demo 消费已准备的本地原生镜像身份，不选择替代镜像，也不维护第二份 ARM 摘要列表。

CI 解析精确测试文件名，并按变更 owner 选择覆盖较广的 suite。源码构建先于 prepare 完成。源码模式 CI 一次性准入候选 ref 与基线 Tag，并固定其已验证输入。相关产品输入未变更时，CI 可以复用基线产品，同时仍执行候选测试和 helper。记录的源码身份是来源证据，不是独立测试选择器。新发布版中，每个 owner 的产品、打包测试、helper 和指南来自同一所选 owner Tag；exact-assets 模式消费已交付材料。历史发布版保留其真实记录的来源和证据。CI prepare 在无 Go/Rust 工具链和组件源码树的隔离运行环境中执行。每个 suite shard 调用同一打包 runner；storage、snapshot 及 ARM image 验收也使用隔离运行环境，prepare 和执行镜像 ID 及环境检查绑定到结果；结果必须包含所有已选用例的成功退出及一致的不可变输入身份。历史 ARM 计划选择原 accelerator 与 guest-runtime 子集；schema 4 按能力分配完整的 suite 所选集合。static lane 执行零个产品用例，不算产品 E2E 验收。

## 静态执行需求

新格式 owner 在 `test/e2e/lib/requirements.json` 中声明 `{"schema":1}`；
platform 使用 `test/e2e/platform/lib/requirements.json`。每个用例在 shebang
之后、shell 语句之前放置唯一的最小执行需求注释：

```bash
#!/usr/bin/env bash
# e2e-requires: ["kvm","tun"]
```

没有可选内核或服务能力需求时显式写 `# e2e-requires: []`。声明包含共享 helper
及实际产品路径引入的需求。普通 Linux 工具、root 权限和 prepared 输入仍属于执行器契约。
不要把注释中提及的设备，或用例故意验证缺失的服务，写成前置需求。

受支持的执行器提供包括 Redis 在内的公共 E2E 用户态依赖，以及具有权限的 mount/PID
命名空间操作。这些属于执行器基础前提，不是可选的调度能力。无法满足这些前提的受限
容器，不会仅因标签匹配就成为合格执行器。带凭据的 OBS 仍需显式选择 suite；能力声明
不会选择该 suite，也不会提供凭据。声明应描述默认 prepared fixture 路径，而不是
`IMAGE_A`/`IMAGE_B` 等可选开发覆盖参数。

| 能力 | 必需操作 |
| --- | --- |
| `kvm` | 打开 KVM API 12 并创建 VM |
| `tun` | 创建并关闭 TAP 设备 |
| `userfaultfd` | 打开 userfaultfd 并协商 API |
| `bpf-map` | 创建、固定并移除 BPF map |
| `bpf-tc` | 加载 SCHED_CLS 程序并挂载到 TC ingress |
| `netns` | 创建网络命名空间和 veth 对 |
| `geneve` | 创建内核 GENEVE 接口 |
| `systemd` | 连接 systemd PID 1 管理器 |
| `journald` | 访问原生日志 socket 并完成同步 |
| `cgroup-v2` | 创建支持内存控制的可写子组 |
| `docker` | 连接执行环境中的 Docker daemon |

缺失、重复、格式错误或未知声明均失败；解析器不执行 shell 表达式。resolver、包校验和
prepare 同时绑定用例字节及规范化需求。`run` 仅在实际运行环境中检查所选需求，并将成功
操作写入报告。检查失败属于环境失败，不能按跳过用例计为成功。产品断言仍负责验收，
启动检查本身不能替代产品通过。

用例增删及需求修改只在所属仓库维护。正常固定源码选择会发现新清单，无需修改平台的
用例到 Runner 白名单。首次引入能力名称时需要补充共享定义、检查和 Runner 证据。
suite 影响选择及带凭据的 OBS opt-in 不变。完全没有 owner 标记的历史包保留原契约；
混合代际明确失败并提示更新所选源码。

unit、race、vet、源码 helper、UFFD 性能及 working-set 门禁保持独立。`test/perf/warmpool-dedup.sh` 在 `make perf-warmpool-dedup` 下保留 warm-pool 表征、数据与测量断言，不属于 correctness suite。组件源码门禁仍由各自仓库维护。

主仓的重要断言归属如下：

| 契约 | 维护入口 |
| --- | --- |
| 拒绝外部监听者且不杀进程；精确 owner reset | `basic.demo.sh` 及 Demo safety helper |
| 重复 prep、root 所有权、0700 目录和 0600 状态 | `basic.demo.sh` |
| 真实 Build/COPY、Quick Start 生命周期、SDK exec/files、模板 fan-out、迁移与数据 | `basic.demo.sh` 调用文档中的 `test/demo/demo_e2b.sh` 流程 |
| stop/start 持久状态、过期 socket、清理失败状态 | `basic.demo.sh` 及 Demo process/state 源码测试 |
| 精确 wheel-only SDK、隔离导入和不可变输入 | 公共 prepare 与 `test-e2e-tool-paths.py` |
| 有界恢复诊断、脱敏、原始退出/清理语义、描述符关闭 | 主仓诊断 helper；`snapshot.read-recovery.sh` 与 `orchestrator.cluster-recovery.sh` |
| warm-pool dedup 工作负载与测量 | `test/perf/warmpool-dedup.sh` |

发布输入、前置条件和验收证据见 [QUICKSTART_zh.md](QUICKSTART_zh.md)。
