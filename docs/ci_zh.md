[English](ci.md) | [简体中文](ci_zh.md)

# 持续集成

## 1. 公开调用方与受信任控制

平台仓维护 `ci-entry.yml`、`integration-tests.yml` 和 `integration-architecture.yml`。
组件 PR 保留 `ci-entry.yml@main` 薄入口，由 GitHub 在每轮固定框架版本。
所有 job，包括 admission、协调、发布、finalize 和 cleanup，都在分配 runner 前检查可信事件中的实际调用仓库：

```yaml
if: github.event.repository.visibility == 'public' && github.event.repository.full_name == github.repository
```

公开的 reusable workflow 提供方、候选输入或 companion 不能授权 private/internal/未知调用方。
非公开调用不分配 job，也不构成成功验收。公开调用方可以依据可信 profile 政策，
为能力匹配的补充 E2E 分组分配 Runner。接入及运维资料见 [ci/runner](../ci/runner/README_zh.md)。

组件调用方和平台 fork 使用可信 main 上的 `pull_request_target` 控制流程，只接受 `main` 和 `release/vMAJOR.MINOR.x`。非 draft 的组件同仓 PR 自动准入；
fork 准入仍查询当前 active 组织成员身份，不能以 `author_association` 代替。
admission 复核当前 PR/base/head 和 integration commit 的两个父提交，并向该 integration commit
写入 `kuasar/ci-exact-head=pending`。draft、外部 fork 和冲突都不能得到 E2E 成功结论。

平台同仓 PR 使用 `pull_request`，在精确的两父 merge commit 上调用本地 reusable workflow，使框架变更在合入 main 前验证自己的 prepare/run 合同。
源码、build、prepare 和产品执行只接收只读 job token，不继承 App secret。所有选中阶段（包括源码/UFFD 和性能）均必须通过，integration result 才能成功。
draft 不分配产品 job。组件和 fork 调用方保留既有可信 admission/finalize。

必需的 `ci / finalize` job 汇总平台 PR 的完整结果，并在成功前复核当前 source set。它只使用只读 API 权限，不需要独立的 commit-status 发布器。

Admission 失败或 Draft 延迟执行时，可信 finalizer 不得成功结束。除 PR 元数据和 integration commit 的有序双亲外，还会重新读取主候选目标分支 ref，避免缓存的 PR 响应让旧 base 被误验收。可信 finalize 还复用当前 source-set 校验器比较有效 companion 声明；无关正文编辑不会使候选失效。每个已选择的源码/架构阶段都必须实际成功；Actions 的 skipped 或 neutral 状态不是产品验证。服务端 required-check 配置与工作流结果是两个独立控制点，需要一起核对。非成员 Fork 的处理见[外部贡献接收流程](../CONTRIBUTING_zh.md#ci-准入与外部贡献)；重跑被拒绝的 Fork 不会改变作者成员身份。本契约不新增批准票数或特定评审机器人要求。

App key 和短期控制 token 只进入受信任的 admission/finalize 与发布控制 job。
产品/helper 构建、源码检查、prepare、E2E 都不接收 App key，候选代码在新的公共 job 或补充 E2E 分组所属的新 Workbench 实例中执行。
公开发行源码按所选 Tag 获取；候选分支/PR ref 按自动观测的身份准入，并通过固定运行 artifact 传递。checkout 不保留凭据；Actions token 只用于可信 API/下载步骤。
publish 使用独立 job 和写权限。

跨仓原子变更沿用双向 companion 标记：

```text
<!-- kuasar-ci-companions
kuasar-sandbox/orchestrator#227
-->
```

一个 PR 最多一个标记，每个允许仓库只能出现一次。companion 必须公开、open、Ready，
具有当前两父 merge ref、精确 base/head，且 head 仍属于组织仓分支。
admission、执行前解析、finalize 均复核。主 PR 的状态不能代替 companion 自己的实际调用方验收。
标记文本编辑需新 head 或 draft-to-ready 事件；合并前核对本轮 plan 与当前标记。
已合并 companion 从后续 PR 标记移除并重跑。只有全部所选阶段成功且精确源码集合仍未变化，
finalize 才能成功；skip/cancel/failure 不能成为合并依据。

## 2. 精确基线与受影响产品

产品影响选测也包含消费者：accelerator/runtime 改动若重建 `flatten-ctl`，增加 telemetry 用例；sandboxer 库改动若重建 `node-ctl`，增加 `orchestrator.resource-*`。这些用例使用 plan 固定的测试 owner revision 和匹配能力的 lane，不另选 main。需要原子验证的测试脚本/依赖修复使用上文双向 companion 声明，明确联合候选源码集合。

`source` 模式从主线维护的 `releases/daily-preview.yaml`、或维护线的 `releases/release.yaml`
选择一个已发布且通过声明验证范围的 aggregate。仅允许当前选择或显式前驱恢复中断发布。
plan 固定 aggregate tag commit、六个独立 unit 的版本/SHA、Release/asset ID、大小、摘要和成功的聚合 run。
新双架构 aggregate 的发布说明还绑定验证 profile 与真实制品摘要。

当前维护的 aggregate 尚未发布时，显式前驱仍是经过验证的二进制基线。对于已完整验收的 schema 4 聚合，普通源码 PR 的非候选产品、测试与 helper 均保留该聚合选定的 owner tag；待发布清单不替换这个已验收组合。已准入候选与 companion 仍按原有产品及消费端覆盖规则叠加。新发布组合必须通过独立的 aggregate exact-assets 门禁，才能成为基线。

已认证聚合证据早于 schema 4 时，历史能力迁移保留现有 maintained-tag 路径。每个差异 tag 仍须已有完整双架构发布及精确 tag/commit、大小和摘要元数据，CI 运输这些固定源码并重建产品及消费者。候选清单不能选择这些 tag 或兼容模式；不完整发布、混合需求代际及需求降级仍然失败。runtime 与 kernel 保持独立。

基线或 ARM 资产缺失时明确失败并要求初始化。不会拼接组件 Latest，也不会回退全源码构建。
历史 AMD64-only 发布仍是合法历史发布，只是不满足双架构基线。
见 [首次 ARM 初始化](release_zh.md#first-arm-initialization)。

按候选/companion 与基线源码的实际产品输入差异，通过小型显式映射选择 Go/native 产品及必要链接/内嵌产品。
accelerator flatten 改动进入 `flatten-ctl` 和实际内嵌它的 runtime；`sandbox-init` 改动进入被测 runtime。
runtime 与 vmlinux 保持独立源码身份，kernel 继续复用已有 Makefile 输入投影。
orchestrator 的 `app/`、`config/` 是产品输入；文档或测试专属变化不选择组件产品。

build 可获取 local Go replacement 所需的精确库源码，但不会因此重建所有 sibling 独立 CLI。

tarstream 载体变更还会使用选定的 accelerator 源码重建 `sandbox-ctl` 和 `node-ctl`。新编码载体的读取端必须链接同一库输入；这不会选择 sandboxer 的存储功能或改变其源码版本。

sandboxer 库变更同样会重建 `node-ctl`，因为其管理和发布路径需要读取 Checkpoint 元数据；orchestrator 的源码选择保持不变。工作集测试将 raw ext4 镜像作为 `diff_template` 输入，并保持活动 `diff` 输出尚不存在；恢复时由已捕获的磁盘图创建输出。新建活动文件遵循选定的加密策略，因此本地 `off`/`auto` 耗时对照包含活动文件和载体的成本。

未修改产品的 hash 必须等于下载基线；候选产品 hash 必须等于本轮真实 build 输出。
不要求任意旧编排源码重建与基线天然字节相同。 Guest init 身份按已校验 Runtime 内嵌载荷核对；候选 init 必须进入该载荷，未变基线 Runtime 则保留原内嵌字节。

### 大型下载之前的文档与发布清单验收

源码 PR 解析比较主仓和每个已准入 companion 精确 base、head 与 integration 提交的完整 Git 树。`docs` 档仅允许 `README{,_zh}.md`、`test/e2e/README{,_zh}.md`、`docs/` 和 `test/e2e/guides/` 内普通 `100644` Markdown；platform 另允许 `workbench/README{,_zh}.md`、`ci/runner/README{,_zh}.md` 和 `test/demo/DEMO{,_zh}.md`。`manifest` 档仅增加 platform 的 `releases/release.yaml` 与 `releases/daily-preview.yaml`；可组合上述白名单文档，并执行两类检查。任何候选混入源码、CI/workflow、发布工具、测试、运行配置、未知路径/仓库或非普通文件都保留 full。截断树与空差异也保留 full；缺失精确身份则失败。删除维护清单会使双文件检查失败，未变的 symlink 也不能作为另一份清单输入。

分类政策和检查代码来自 platform 可信基线；存在 platform companion 时使用其 base。候选不能将自身加入白名单。基线尚无新清单规则时，清单变更仍走 full；候选工作流通过完整 source gate 测试自身契约，因此实现可在启用前得到验证。分类先于基线资产、源码归档与构建环境下载；两类轻量档均不执行候选代码。

共享 `ci/check_docs.py` 检查 UTF-8、本地链接和锚点、双语互链与英文默认，已有英文单语文档仍合法。检查修改文档及引用已重命名、删除或修改目标的未变文档，不将无关文档债务扩入本次检查。外部访问和翻译完整性仍需人工验收。

清单检查在精确 head 与 merge 上使用现有发布解析器读取两份维护清单，检查字段、unit tag、Stable/Preview 版本、前驱顺序、双文件一致性和维护线归属。复用现有 aggregate reader 验证声明的已发布前驱及已经发布的当前选择；已发布组件使用现有精确 tag/Release/双架构资产检查。Preview 的源码和依赖绑定仍为必需；已发布依赖改变时，仅当完整源码树中既有链接输入投影一致才可复用消费者。新组件 tag、等待发布的 tag 或 draft 明确记录为待满足发布前提，并保留发布源分支与所选依赖。复用尚未发布的依赖时，记录待执行的链接输入核验。后续发布 coordinator 仍负责冻结未来源码提交并满足这些前提。这是发布计划验证，不表示未来组合已经通过 E2E，也不会在此触发组件发布。

原 finalizer 仅在成功轻量证据匹配当前精确源码集、可信政策、run 和 attempt，且重新核对当前 PR/base/head/companion 准入后，接受有意跳过的重任务。缺失、取消、失败、空或过期证据不能通过。证据保留清单计划、已发布身份与待满足前提。aggregate 的 `exact-assets` 始终保留完整 E2E 和 Workbench 验收。宣称收益前须记录真实 `docs`/`manifest`/`full` Actions 时间。实现 PR 修改可执行政策/工作流，因此自身走 full。回退恢复完整调度，不放宽准入或必需检查。


Go EROFS runtime 的 bundle 只需包含 init、envd 和 flatten-ctl，新的聚合制品
不再收集 mkfs.erofs。历史归档仍可保留该工具；旧 runtime 继续要求其内嵌工具
与选定的外部二进制逐字节匹配。可信读取器依据 flatten-ctl 中链接的外部
go-erofs 模块记录选择合同，无须执行该二进制或安装 Go。宿主 runtime 打包、
独立 fsck 及原生对照测试仍保留所需工具。

### 按能力分配 E2E

计划 schema 4 将应执行用例选择与执行位置分开。它从六个已选 owner 源码树发现头部声明，
固定用例摘要、需求、可信 `ci/integration/runner-capabilities.json` 配置及摘要、
执行器镜像 ID、run/attempt 和确定性分组。schema 2/3 的旧计划与已发布结果继续保留
原 ARM 选择范围和 Workbench 资格含义。

profile 声明架构、Runner 标签、执行器、资源预算、优先级、启用状态以及每项能力证据。
`supported` 可满足需求，`unsupported` 表示已知缺口，`unknown` 两者都不代表。
所有需求均满足后，优先公共 Runner，再按优先级及 profile ID 确定顺序。
每次补充分配必须记录**每个**已启用公共候选至少一项必需能力已知不支持。
公共候选只有 unknown 时阻止分配。资源禁用或不可用不会使必需用例变成可选。
用例元数据不能指定标签、镜像、命令、优先级或凭据。运行时专用容器不支持 `systemd` 或 `journald`，
即使其宿主支持也不能满足该需求。

分组依据 profile、suite 和有效运行环境生成，互斥并集必须等于普通 suite 选择的用例，
带凭据 OBS 仍需显式启用。公共 build、helper、prepare、source 和 performance 的分配
保持不变。source 与 exact-assets 模式都在公共侧一次准备所有分组的并集。
prepare 导出已经构建的运行时镜像并绑定压缩归档摘要；执行前验证归档和镜像配置身份，
再加载镜像。执行分组不重建产品。

补充分组使用现有 Workbench system launcher。宿主 Actions 步骤校验并传输固定输入，
随后在所属实例中调用现有公共 E2E 入口。storage、snapshot 和 ARM image 分组继续使用
嵌套的运行时专用容器，不含编译器或组件源码树。每个分组使用新的下载、prepared、结果
临时目录及唯一 Workbench 实例名，复用自托管机器时也不继承旧任务文件。诊断上传后清理
所属任务文件。Runner 凭据和宿主 Docker socket 不挂入 Workbench。能力启动检查发生在有效运行环境中，结果绑定实际 Runner 名称、系统、架构、
环境类型、run/attempt、profile/配置、prepared provenance 和执行器镜像。
架构及最终门禁要求每个非空分组成功；失败、取消、缺失结果或身份变化均不能变成成功。
空组不分配 Runner，也不宣称完成 E2E。部分重跑只复用同一不可变计划和 workflow run
中的成功产物；回执保留实际生产 attempt。新的失败或取消会阻止收集，不能借用旧成功。

现有平台同仓框架 PR 入口使用只读公共元数据，从已准入事件解析 companion 声明。
执行前和最终验收前均使用原有 merge/head/base 与当前分支校验；该入口仍拒绝调用方
直接传入 companion 记录。

所选组件版本必须先携带需求声明。新旧混合源码集明确失败，不能将缺失声明解释为空需求。
完全历史输入保留历史计划路径；基线升级 schema 4 后不能降级需求格式。
启用标为 `unknown` 的能力前，须通过现有 prepare/run 完成实际产品验收，并在可信配置中
补充证据。不增加常驻能力探测矩阵，也不在失败后自动转投其他资源。
联合 E2E 覆盖不会改变 artifact-only 或单实例 native-full Workbench 资格。
参见 [Runner 接入](../ci/runner/README_zh.md#capability-e2e-runners)
和[用例声明](../test/README_zh.md#静态执行需求)。

## 3. 两条独立架构生命周期

```text
resolve → x86 build → x86 prepare → 原生 x86 shards → x86 result
        → native ARM build → ARM prepare → 原生 ARM shards → ARM result
        → 独立源码 unit/race/vet 与 UFFD 检查
                                  全部所选结果显式汇总
```

两个架构分别选择原生 Runner (`ubuntu-24.04` 或 `ubuntu-24.04-arm`), 通过 `.github/actions/workbench` 调用已有 Makefile/native recipe. 受信 resolver 每次运行只选择一次已发布且验证通过的 Workbench, 固定两个架构的 registry digest、image config ID 和 framework SHA. 后续 job 核验同一选择、实际镜像架构及 release/source 标签. 普通 PR 仍只构建准入的产品差异和必要 helper. 每条 lane 独立 prepare 一次, 不等待另一架构构建; 所有 shard 和架构结果保留独立身份并显式汇总.

构建复用 `workbench/workbench` build 模式: ordinary UID、只读根文件系统、移除 capabilities, 不挂载宿主 Docker socket 或 KVM. 私有源码位于 `/src`, 受信框架只读挂载至 `/inputs/release`. `HOME=/work/home`、磁盘上的短路径 `TMPDIR=/build/t`、Go/Cargo 状态及 `/build/native-cache` 都属于任务. CPU/内存预算明确传给 Go、Cargo、CMake 和 native make recipe, 不把 CPU quota 当作 `nproc`. 发布凭据留在宿主编排侧; publisher 校验器和 archive readers 使用单独禁用缓存的 Workbench 调用.

继续验证目标 ELF/kernel Image、静态链接和包布局. helper 与打包工具均使用目标原生架构. Workbench producer 直接在原生 Runner 构建自身.

prepare 在组装前校验包路径/类型/权限/归属、摘要、必要产品和 runtime 内嵌身份。
两个架构独立解压。source 模式覆盖六个精确测试 owner 的完整目录，组装为扁平用例目录和按 owner 命名的底层库；
exact-assets 模式直接消费 platform 包中的同一布局。两条路径都拒绝旧 owner runner 和重复用例 ID。
按 owner 记录 test revision，与可信 framework SHA 分开。

新聚合清单只选择 owner/unit Tag。用例、运行库、helper 源码和指南均来自所选 Tag；guest-runtime 跟随 runtime Tag，kernel 保持独立的 vmlinux Tag。解析器对实际干净源码核验 repository、tag、commit 和 tree，并写入 `source-records.json`。SHA 仅是自动来源证据，不提供另一套选择入口。

源码 CI 对现有 PR merge ref 和受信任分支 ref 核验自动观察到的身份，然后通过 `source-inputs.tar` 固定并传递 Git 源码树。构建、helper 和必需源码检查恢复同一输入。非候选 owner 使用所选单元 Tag；候选及 companion 测试即使没有产品差异、所有产品字节复用已发布聚合也仍执行。产品相关输入和独立 kernel 差异投影不变。

对于没有准入候选的 owner，所选 Tag 必须保留已验证基线中的每个用例 ID。缺少用例时明确失败，需要经评审的新 Tag 选择；源码 CI 不得静默缩减覆盖范围。

历史消费者按聚合 Tag 获取清单及发布包，保留原始独立测试来源。新发布 binding 内嵌 canonical validation plan、owner 用例映射和源码记录，与包摘要共同绑定。旧 binding 缺少归属信息时，必须获取其真实保留的 integration-plan artifact，核验 canonical identity 等于 `plan_id`，并交叉验证所有绑定身份与资产。证据缺失或歧义时明确失败，不从文件名猜测历史归属。

产品合同为预构建产品 → `e2e prepare` → `<suite>.<case>.sh` → 共享公开入口 `e2e run`。完整文件名就是用例 ID，
首段只能是 `basic`、`storage`、`image`、`network`、`sandbox`、`snapshot`、`orchestrator`、`builder` 或 `telemetry`。
内部 CI shard 按 suite 分组精确文件，plan 在执行前记录所选用例和架构排除项。

source/helper build 从已准入的 owner 声明派生完整 helper 清单和配方，并将契约绑定到输入计划。通用构建器使用固定依赖工作区执行各 owner 配方，拒绝缺失或额外输出。内部二进制名称、包路径和构建目标由组件维护。prepare 校验预期契约、架构、源码身份、字节和安全权限，提供声明的环境变量别名与 `E2E_HELPERS_DIR`，不执行编译。包含静态无 libc cgroup probe 在内的用例必要能力仍单独检查，不作为完整允许清单。发布包携带两种架构的 helper 和相同 owner Tag 的证据。同一更早的 build 阶段按 `test/demo/requirements.lock`
获取 Python 3.12 的完整 Demo SDK wheel 依赖闭包，固定全部版本及 wheel 摘要；发布包携带两种架构的 wheelhouse。prepare 消费这些二进制，
准备 manifest archive、guest flatten fixture、固定 image ID/digest、orchestrator 基础镜像，并仅从本地 wheelhouse 通过
`--no-index --find-links` 和 `--require-hashes` 安装 Demo SDK。wheel 缺失或变化时在安装前失败。provenance 绑定包名、版本、wheel 摘要、lock 身份和安装后的文件树。
它调用与下载发布包相同的公开 prepare 入口，并检查已有产品/测试输入没有变化。Capture helper 测试保留在独立 orchestrator 源码 gate。
工作区包含 `bin/`、`test/`、`fixtures/`、`images/`、材料及 `provenance.json`。真实 Build、flatten、snapshot、publish、restore 仍在聚焦的产品用例中执行。

E2E 只 checkout 可信执行器并下载目标 prepared workspace，执行前后核验全部文件摘要和权限。
它不 checkout 组件源码，不隐式进行产品 Go/Cargo/kernel 编译。
每个 shard 使用短路径、磁盘支持的私有可变目录，socket、direct I/O、Docker 配置、性能状态均在不可变输入之外。
源码依赖的 connector/sandboxer/orchestrator unit/race/vet、真实 pinned-BPF 统计、ENOSPC、Collector/usage harness 回归和 UFFD benchmark 保留为独立必需源码 job。
source 模式 x86 sandboxer/platform 还在独立性能 job 中，用同一组制品保留 A/B/C/D `off/auto × cold/warm` working-set smoke。

完整源码门禁在原生 x86 的独立 Workbench system 实例中运行, 包括混合 unit/race/vet 与真实特权检查所需的编译器. 受信宿主从固定准入 artifact 恢复 `plan.test_revisions` 对应源码, 实例接收 `/src` 私有副本, 不接收宿主 Docker socket、凭据或 ordinary 构建缓存. 每个精确测试 pin 的原 owner 脚本与 Make target 继续决定实际检查, 包括已发布的旧布局. 它们在实例内以 ordinary UID 执行, 保留不可读文件及拒绝提权的检查. 源码进程及子进程保留原 Hosted affinity 策略: 按已声明的 `KUASAR_BUILD_JOBS` 预算选择实际允许的 CPU, 同时受实例 CPU quota 限制. 私有 sudo 配置保留原脚本的显式升权, 仅既有 TAP/netns 性能 fixture 通过 `sudo make test-perf-tools` 执行. 该用户仅访问实例自己的 Docker socket, 不修改宿主账号或策略. 真实 `sudo`、systemd、BPF、mount namespace、UFFD 与私有 Docker 保留原参数和断言. 门禁开始前必须成功创建 TAP 与 network namespace. 结果保留 framework/test/image 身份、各命令、退出码及耗时, 失败证据经所属实例清理入口收集. 产品、独立 helper 与发布构建使用 ordinary UID 的 build 模式. 宿主 bootstrap 保留编排及产品执行前提, Workbench producer 继续直接在原生 Runner 构建.

CI 启动已选 prepared case 时直接提供 root 权限和可信工具路径。跨 `sudo` 只传递显式准备的输入（包括私有状态目录）；执行不读取生成的 owner-runner registry。

所有非空 lane 都在没有 Go、Rust、C/C++ 编译器及组件源码树的运行时容器中 prepare。完整 storage 和 snapshot 套件也在该容器中执行，
覆盖非 KVM 和 KVM 合同；适用的原生 ARM image 用例使用相同边界。需要 systemd 的用例在所选原生 host 或 Workbench system 实例中执行。
可信预检和结果记录绑定不可变运行时镜像 ID，以及编译器/源码树缺失的验证证据；零用例静态 lane 不算产品验收。
容器输入只读，可变用例目录和输出目录独立。

`snapshot.read-recovery.sh` 失败时，在 cleanup 前将用例、阶段、失败行及有界固定词表错误证据保留在 job 日志中。诊断保留测试的原始退出状态。

现有 `make test-release-tools` 入口在源码 CI 上下文或任一物化文档存在时，还执行固定源码集的发布验证；此时 `.source-plan.json` 和 `.source-checks.json` 均为必需。入口接受原始 `source` 和 `exact-assets` plan，绑定未修改的 plan ID、干净源码树和现有 Go workspace。组件保留所选 tag 身份，候选保留已准入的 ref。对于 `exact-assets`，尚未发布的 platform 源码通过原始仓库、commit 和 tree 绑定暂存选择、固定运行输入、物化回执和 helper 声明。入口先验证这些输入，再串行执行所选组件原有的 `make test-release`；platform 候选会选择全部五个组件。入口同时执行所选 owner 声明的原生 helper 配方，校验完整输出，并在 `KUASAR_CI_DIR` 下记录命令、退出码、声明、依赖身份和二进制摘要。执行后源码及 workspace 输入必须保持不变。已认证的历史 helper 证据保留为历史事实，不宣称重新执行过其配方。该原生源码回归不替代各架构构建、prepare 或 E2E 验证。

独立运行 `make test-release-tools` 仍保持平台工具验证范围，未请求固定源码集验证时会明确说明。对已物化的本地源码集，可在保留现有 Go workspace 和工具链环境的情况下执行 `make test-release-source-set ORG=<fixed-source-root> RELEASE_CHECK_OUTPUT=<fresh-evidence-directory>`。该显式入口始终要求两份准入文档，不重新获取源码。

### 源码检查分组

`run-source-checks.py --group all` 仍是默认完整入口。A 组保留平台工具契约（完整的 `make test-ci-tools test-release-tools` 调用）、特权性能工具契约、按原条件选择的 connector/sandboxer 脚本、accelerator fixture 和原有 UFFD 检查。B 组执行所选 orchestrator 脚本。选择条件、命令参数、工作目录与依赖闭包均与 `all` 相同；未选择 orchestrator 时不调度 B。每组使用独立 Hosted 机器和新的 Workbench system 实例，保持原 2 CPU/8 GiB 预算，独占源码、Go workspace、临时目录、构建和模块缓存。组间不恢复或共享源码缓存。

可复用工作流仍以 `source_check_groups: false` 作为兼容默认值。PR 调用入口 `ci.yml` 和 `ci-entry.yml` 在 rollout 验收后选择 A/B；聚合调用方在独立证明整条工作流收益与成本前继续使用 `all`。将任一 PR 入口恢复为 false 即可回退 `all`，不改变检查目标、资源预算或必需检查。

两轮真实 Actions 对照在每轮内使用相同的已选组件源码、用例清单、完整命令和不可变 Workbench 镜像，候选文件树仅调用入口的分组开关不同。两轮都是没有恢复可写源码缓存的全新冷构建，重复运行也不宣称暖缓存。这些首次执行轮次的观测记录最终工作流反馈和总任务分钟，不使用历史命令模型或后续故障/重跑成本。

| 对照（all / A+B） | 最终反馈 | 总任务分钟 | 反馈 / 成本变化 |
| --- | --- | --- | --- |
| [38543260895](https://github.com/kuasar-sandbox/kuasar-sandbox/actions/runs/38543260895/attempts/1) / [38543242866](https://github.com/kuasar-sandbox/kuasar-sandbox/actions/runs/38543242866/attempts/1) | 37:43 / 25:28 | 99.467 / 104.550 | -32.48% / +5.11% |
| [38549209231](https://github.com/kuasar-sandbox/kuasar-sandbox/actions/runs/38549209231/attempts/1) / [38549216882](https://github.com/kuasar-sandbox/kuasar-sandbox/actions/runs/38549216882/attempts/1) | 44:09 / 27:06 | 108.983 / 110.450 | -38.62% / +1.35% |

第二轮分组运行后来的取消属于单独的负向测试，不能用该 run 当前结论覆盖首次轮次的成功测量。完成 rollout 前须重新验证当前源码正确性和全部六仓入口，包括失败与局部重跑。这些 SOURCE 数据不证明聚合流程或 Workbench 分组收益。

两种调度均将结果绑定精确 plan、原始命令覆盖、Workbench 镜像、成功清理，以及当前 Actions 生产任务和产物 ID。产物名包含 run、attempt 和 group，不覆盖其他轮次。生产任务失败、取消、缺失或重复均阻止最终通过。较早轮次的成功结果仅在仍为该组最新生产任务且匹配当前 plan 时可复用；重跑生产任务后，其旧产物失效。A/B 不得共享实例或机器。回退选择 `all`，保留这些检查，不改变发布选择和必需检查规则。

部分重跑时，Actions 可能为沿用的成功任务分配新 ID，但保留原执行时间、runner 和步骤。共享读取器仅在完整执行身份匹配时追溯到最近的前序成功生产者；真正较新的运行中、失败或取消任务仍使旧证据失效。此规则同时覆盖源码组、Workbench 运行组、ARM、暂存输入和验证任务。

### Workbench 运行分组

可复用工作流和聚合 dispatch 提供 `workbench_runtime_groups`，默认 false，保留 x86 完整运行任务及 Hosted ARM 产物级验收。实验性 x86 分组为 A（`basic`、`image`、`orchestrator`、`sandbox`、`snapshot`）和 B（`builder`、`network`、`storage`、`telemetry`）。成员由本轮 plan 的精确普通用例清单派生，未知套件失败，覆盖必须完整且不相交，`storage.obs.sh` 的既有排除不变。套件顺序与状态保留在同一组内。每个独立 Hosted 机器和空的私有 Workbench 实例均准备**全部**普通离线输入，再以原 2 CPU/8 GiB 预算执行分配用例，保留 preflight、外网、隔离、耗时、磁盘和清理检查。

分组证据显式声明 `workbench-groups-v1` / `native-grouped`，保留两次独立执行、共同的精确源码/资产/镜像身份、完整覆盖及不可覆盖的 run/attempt/group 产物。当前成功生产任务、ARM 证据、stage/plan 输入与最终验收任务必须一致。未变化的较早成功轮次可复用；任何已绑定生产任务重跑均使旧验收失效。缺失、重复、失败、取消或共享实例的证据不得授权发布。

必须先合并兼容读取器，再生产分组结果：发布器从 **main** 加载 `bind-validation.py`。Registry 发布、Release 正文、基线/产物解析、Hosted 选择和 Preview 恢复同时支持历史 full、ARM 与分组证据。公开下载仍使用相同资产和按架构选择，正文上限及完整 registry 预留不变。启用前须用可比较的多次真实 Actions，覆盖失败/重跑注入、排队/准备/执行/最终反馈及总任务分钟，证明最终收益和成本合理。本地或模拟元数据不算 Actions 验收。缺少稳定最终收益或成本过高时保持 `all`；回退停用生产，但保留已发布分组证据的读取能力。

需要在相同源码上重复验收时，聚合 dispatch 还接受 `validation_only=true`（默认 false）。它仍要求 main、精确当前选择及尚未发布的版本，并执行原有全部构建、运行、源码检查，以及发布绑定、正文上限和 registry 输入验证；仅在 registry/Release 写入前结束，并按原到期时间保留本轮产物供失败/重跑测试。它使用 `Validate aggregate <version> @<sha>` 标识和按 run 隔离的 `aggregate-validation` 并发键，使独立的 `all` 与分组验证可与普通发布并存。验证运行（包括旧的 `[validation only]` 后缀标题）不计入活动发布、发布重试、恢复候选或成功发布基线。普通发布与 delete-preview 继续共用 `aggregate-mutation` 锁。验证在归档、发布正文和 registry 输入消费者执行前后均检查版本尚未发布。第二次检查紧邻成功报告之前；如果此时 API 报告 Release 已发布，验证必须失败。这是最后一次 API 观察，并不原子排除该观察之后发生的发布。对照必须使用同一选择，且该选择仍为分支的精确当前 HEAD，发布前计时不能代替真实发布的端到端收益。最终通过正常发布模式完成所选版本；不得仅为取得基准数据而重跑已发布分组证据背后的生产任务。

## 4. Daily 与 Stable

`exact-assets` 仅接受实际公开的平台 aggregate workflow。
plan 固定提交清单与暂存的十四项资产（platform + 十二个组件架构包 + SHA256SUMS），不选择产品或 helper 重建。
各 target 与 PR 共用 download/compose/公开 prepare/公开 run/result 原语，并直接消费精确暂存 platform archive 中的预构建 helper 包。
必需源码检查始终与 artifact E2E 分离。

publish 再次核对双架构成功结果与暂存摘要，原样发布归档，不重新编译。
许可证/材料、源码身份、受信任 publisher 标记及版本不可变约束继续适用。
精确 ARM 选择与能力分组在执行前及 aggregate 验证绑定中声明；历史非 KVM 证据仍限定于原范围。

## 5. Native cache

`ci/native-cache/native-cache.sh restore-or-build` 处理:

- guest `vmlinux`;
- guest `envd`;
- RocksDB headers 与 `librocksdb.a`;
- patched `cloud-hypervisor`。

缓存路径为 `$KUASAR_NATIVE_CACHE_ROOT/v3/<arch>/<component>/<input-hash>/`. Workbench action 通过 Actions cache 传输 native、Go modules/build cache、Cargo registry/Git/material 下载, 实际操作受调用者的 runtime cache 权限限制. key 区分架构、精确候选来源和声明的构建覆盖范围. candidate namespace 绑定实际 PR、companion、产品、测试/helper 与内核输入. 精确 key 和 restore 前缀都区分 helper、已选择的产品集合及各发布单元, 防止不可变的部分缓存占用另一构建范围的 key.

启用缓存的 Workbench action 在恢复后、owner 命令前运行 `go clean -testcache`. 它仅使旧 Go 测试结果过期, 保留编译和模块缓存, 避免恢复的成功结果替代当前 Job 的测试执行. Workbench receipt 记录该命令及退出码; 失败会阻止 owner 执行, 原有清理仍会运行.

Actions cache 的存储和访问属于调用者仓库及 ref, 遵守 GitHub 的缓存规则; 同 key 不会跨仓共享. 默认分支 dispatch 的候选输入仍属于 candidate. trusted cache 写入要求在源码执行前确认干净、精确的公开源码提交属于对应 main 历史, 结论保存在候选挂载之外. 现有受信 main/release 入口在 runtime cache scope 可写时提供写入路径, 沿用现有权限. publisher 可执行文件不消费候选或构建缓存.

聚合 helper 从固定源码 artifact 恢复与产品相同的所选单元 Tag 的干净 Git 源码. Runtime 发布在安装已验证的预编译资产前冻结同一宿主 receipt, 避免把生成的二进制和 notice 当成源码改动. 原有资产验证及实际工具链/recipe 缓存 key 继续适用.

组件 `pull_request_target` job 的 cache 权限为只读. save 尝试可能报告 `cache write denied: token has no writable scopes`, 同时 cache action step 本身仍成功; 这不代表缓存保存成功. Workbench receipt 记录请求的缓存身份及 restore 结果, 不证明已写入. 缓存验收必须核对目标仓库/ref 中真实成功的 save, 再由全新 Job 恢复并验证确切产品和打包材料. 其他仓库或事件下的可写缓存不能替代这条路径的证据.

native key 覆盖 recipe、patch/config、上游内容、架构、实际工具链/ABI 和有效编译参数. 完整 Workbench/image/framework 身份保留在 provenance, 每日镜像标签不单独导致 miss. synthetic import/kernel 默认用户、主机和日期在实际构建时规范化, 显式覆盖仍有效. 固定容器路径保留 Cargo 源码和 linker 身份; 随机任务目录不进入 Actions cache path version. 写入沿用校验和与原子 rename; restore 再核验 descriptor/payload. 正常 miss 按原 recipe 构建, 匹配但损坏的条目失败, 不静默重编或原地修补.

EROFS 写入已切换到 Go 生成器，项目不再维护补丁、mkfs 编译或相应 native cache。Runtime bundle 打包使用 Workbench 预安装的宿主 mkfs；可信读取工具只构建 fsck/dump，不导出 mkfs。旧制品仍可读取。

Envd 保留匹配的 Go workspace 源码上下文. Cloud Hypervisor 保留 patched 源树、原始 build report、linker map、Cargo lock/metadata/source manifests 和许可证; Cargo registry/Git 与已验证的 release-material 下载随 native 条目配套恢复. 恢复后的 package 必须使用这些确切材料通过 validate, 不替换旧证据或静默重编. 材料不完整的旧 schema 产生 miss. 仓库补丁仍来自准入 source set, 独立打包验证器保留访问精确 Git 对象的能力.

同 key 构建和恢复持有条目锁。每组件默认保留最近使用的 4 个 key,且只回收超过保护期并能
非阻塞取得锁的条目。缓存测试入口:

```bash
make -C kuasar-sandbox test-ci-tools
```

## 6. Hosted 前置条件与证据

普通组件/helper 编译、构建期测试与发布打包使用固定 Workbench. 宿主负责源码准入、传输及发布. 受信 EROFS/runtime readers 在单独 Workbench 调用中导出给无编译器的 prepare/E2E job, 后者保留原有运行库及架构/能力声明.

`artifact-x86` 保留 Docker/systemd/cgroup v2/KVM/UFFD/netns/BPF, `artifact-arm` 只提供既定非 KVM 执行条件. x86 VM bootstrap 保留已有每 job KVM udev/group 配置并检查实际 KVM/UFFD 访问. 缺少必要能力或断言失败都会使 job 失败. Workbench receipt 记录镜像获取、预算、命令、退出码、缓存 scope 与 cleanup, GitHub job/step 时间提供 restore/save 和传输成本. 只清理本任务实例; 上传前拒绝越界 symlink 和特殊文件, 保留诊断后删除实例缓存和输出状态.

证据 artifact 包括 `integration-plan`、每架构 `integration-provenance`、每 shard `integration-shard`、
`integration-source-result`、两个 `integration-architecture-result` 和最终 `integration-validation`，均带 run ID/attempt。
provenance 记录基线资产、产品来源/hash、精确源码/测试/框架、内嵌载荷、实际工具/native key、helper/fixture hash、权限及既定用例选择。独立 working-set 结果使用 `integration-performance`；
干净环境的 prepare/run 结果记录经验证的运行时环境。
结果记录所选用例、退出码、耗时和 prepared provenance 摘要。
聚合 cleanup 仅移除大型 build/prepared/stage 传输物，验证元数据保留七天；不上传含测试凭据的原始运行状态。

轻量合同检查沿用 `make test-ci-tools test-release-tools test-perf-tools`，需可信 EROFS readers 与 `KUASAR_RUNTIME_READER`。
组件 release/workflow/fixture 检查保留原入口。开发者 `make test-e2e RELEASE_DIR=... E2E_WORKDIR=...`
封装相同的公开 prepare/run，不编译产品或 helper。源码构建、helper 和性能目标保持独立。
合同 fixture 和原生预检不能代替实际公开标准 runner 验收。

## 7. 首轮覆盖与切换

架构/owner/用例、缺失原因、已执行证据和后续事项集中在唯一的 [首轮覆盖表](ci.md#7-initial-coverage-and-rollout-evidence)。
以下历史 ARM 验收仅选择 accelerator 和 guest-runtime 已有独立非 KVM 套件，其余 owner 当时未选 E2E；schema 4 的能力路由不改写这些历史证据。
ARM VM/KVM/restore/Builder/cluster 同等覆盖、新硬件和全面用例扩充属于后续工作。
已选测试失败不能事后改为 unsupported，整个 ARM job 不使用 continue-on-error。

[聚合 run 35751885794，attempt 1](https://github.com/kuasar-sandbox/kuasar-sandbox/actions/runs/35751885794)
已通过两个既定 profile，并从源码/框架 `69c26d2e9d7a494b3463e42c3b8c20ac1119de4f`
发布 [release-v0.1.5-preview.20260922.4](https://github.com/kuasar-sandbox/kuasar-sandbox/releases/tag/release-v0.1.5-preview.20260922.4)。
发布验证绑定记录 plan `00969ac8d0bd389b78101ece51675c9315ce2d985beaf6c18b36a8169dcc700b`、
各 owner 独立测试 pin、两架构 provenance 摘要和成功 shard 结果。
十四项发布资产与测试时暂存摘要完全一致，匿名读取 SHA256SUMS 验证通过；
六个组件 unit 全部复用，十八组历史 Release/tag/asset 身份保持不变。
x86 的全部六个 owner、独立源码检查和 UFFD benchmark 均成功；原生 ARM 的 accelerator/guest-runtime 非 KVM 套件成功。
标准 x86 使用 `ubuntu-latest`，原生 ARM 使用 `ubuntu-24.04-arm`。逐项 job 链接见同一覆盖表。

[PR #129](https://github.com/kuasar-sandbox/kuasar-sandbox/pull/129)
已在提交 `dea0bc66ae766caf47f3c6684a7f426b79b730ba` 将共用工作流切换到 `integration-tests.yml`，
删除临时 artifact 别名与旧源码编排；组件薄入口仍使用 `ci-entry.yml@main`。
其[合并前必需运行](https://github.com/kuasar-sandbox/kuasar-sandbox/actions/runs/35756663913)
使用旧受信任框架，通过源码 E2E、UFFD 和 working-set smoke。
该源码验证与 artifact PR 验收分别记录：每个真实 caller 均保留实际解析的 framework SHA、
baseline/delta plan、prepared-workspace 执行、两架构结果及适用的 working-set smoke。
最终 caller 证据在 [#152](https://github.com/kuasar-sandbox/kuasar-sandbox/issues/152) 中记录后再完成上线结案。

accelerator、connector、sandboxer、orchestrator 已切为 Public，并读回可见性与匿名源码/资产访问结果。
四组件以及平台、guest-runtime 均已通过各自真实 Public 源码 CI 后正常合入；精确运行链接见上方覆盖表所在章节。
历史测试访问能力的边界已在 [#82](https://github.com/kuasar-sandbox/kuasar-sandbox/issues/82#issuecomment-5765674410) 结案：
对应 fixture 使用每次调用独立的 loopback/本地访问能力，原实例已销毁，复制的 fixture 只会创建独立本地实例。
普通评审和必需检查继续生效。

## 8. 参阅

- [发布规约](release_zh.md)：版本选择、ARM 初始化与资产发布。
- [部署](deployment_zh.md)：运行能力与服务。
- [Runner 运维](../ci/runner/README.md)：既有持久基础设施。
- [测试快速开始](../test/QUICKSTART.md)：开发者 E2E 条件。

补充自托管 E2E 从可信 resolve 作业生成的小型同运行归档恢复固定范围的稀疏执行器。Git 将原始 commit、tree 和 blob 对象打包到新仓库，不复制远端配置或凭据；export 属性不能改写或遗漏受控文件。resolve 通过工作流输出传递归档 SHA-256 和预期 Git tree。在执行归档内任何代码之前，启动步骤核验该摘要和精确工作流 SHA；恢复步骤继续核验 commit/tree、全部 Git 对象、受控文件一致性及原有稀疏范围。符号链接、gitlink、特殊权限、超限归档和不安全路径立即失败。固定版本的官方下载 action 启动步骤限时五分钟，恢复限时三分钟；公共托管 E2E 保留原 checkout。这减少补充 runner 上重复的 Git 连接和延迟 fetch，prepared 产品仍使用下述有界本地下载器。

架构作业通过本地 action 下载同一次运行的 artifact。每次尝试从解析元数据之前开始计时，600 秒硬时限覆盖响应头、传输、压缩归档 SHA-256 校验和解压；最多尝试三次，退避 15/30 秒，每次使用新的签名 URL、连接和独立临时目录。首次成功取得元数据后固定 artifact ID、生产作业、大小和 digest。认证、证书、身份、digest、归档及未知错误立即终止；仅明确分类的临时传输错误和硬超时可以重试。取消先终止并回收工作进程再清理，整个选择集校验成功后才原子移入目标目录。步骤上限为 35 分钟，现有作业总时限继续生效。日志仅记录阶段、尝试次数和耗时，不输出运行时令牌或签名 URL。Node 入口接收 runner 提供的同运行身份并调用 Python 标准库实现，不安装依赖，也不扩大工作流权限。
