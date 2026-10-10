[English](ci.md) | [简体中文](ci_zh.md)

# 持续集成

## 1. 公开调用方与受信任控制

平台仓维护 `ci-entry.yml`、`integration-tests.yml` 和 `integration-architecture.yml`。
组件 PR 保留 `ci-entry.yml@main` 薄入口，由 GitHub 在每轮固定框架版本。
所有 hosted job，包括 admission、协调、发布、finalize 和 cleanup，都在分配 runner 前检查可信事件中的实际调用仓库：

```yaml
if: github.event.repository.visibility == 'public' && github.event.repository.full_name == github.repository
```

公开的 reusable workflow 提供方、候选输入或 companion 不能授权 private/internal/未知调用方。
非公开调用不分配 hosted job，也不构成成功验收。没有私有 ARM 队列、新私有适配、larger/付费 fallback。
既有 runner 运维资料仍在 [ci/runner](../ci/runner/README.md)，新流程不修改其服务。

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
产品/helper 构建、源码检查、prepare、E2E 都不接收 App key，候选代码在新的标准 job 执行。
公开源码以匿名精确 SHA 获取，checkout 不保留凭据；Actions token 只用于可信 API/下载步骤。
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

`source` 模式从主线维护的 `releases/daily-preview.yaml`、或维护线的 `releases/release.yaml`
选择一个已发布且通过声明验证范围的 aggregate。仅允许当前选择或显式前驱恢复中断发布。
plan 固定 aggregate tag commit、六个独立 unit 的版本/SHA、Release/asset ID、大小、摘要和成功的聚合 run。
新双架构 aggregate 的发布说明还绑定验证 profile 与真实制品摘要。

基线或 ARM 资产缺失时明确失败并要求初始化。不会拼接组件 Latest，也不会回退全源码构建。
历史 AMD64-only 发布仍是合法历史发布，只是不满足双架构基线。
见 [首次 ARM 初始化](release_zh.md#first-arm-initialization)。

按候选/companion 与基线源码的实际产品输入差异，通过小型显式映射选择 Go/native 产品及必要链接/内嵌产品。
accelerator flatten 改动进入 `flatten-ctl` 和实际内嵌它的 runtime；`sandbox-init` 改动进入被测 runtime。
runtime 与 vmlinux 保持独立源码身份，kernel 继续复用已有 Makefile 输入投影。
orchestrator 的 `app/`、`config/` 是产品输入；文档或测试专属变化不选择组件产品。

build 可获取 local Go replacement 所需的精确库源码，但不会因此重建所有 sibling 独立 CLI。
未修改产品的 hash 必须等于下载基线；候选产品 hash 必须等于本轮真实 build 输出。
不要求任意旧编排源码重建与基线天然字节相同。 Guest init 身份按已校验 Runtime 内嵌载荷核对；候选 init 必须进入该载荷，未变基线 Runtime 则保留原内嵌字节。

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

历史消费者按聚合 Tag 获取清单及发布包，保留原始独立测试来源。新发布 binding 内嵌 canonical validation plan、owner 用例映射和源码记录，与包摘要共同绑定。旧 binding 缺少归属信息时，必须获取其真实保留的 integration-plan artifact，核验 canonical identity 等于 `plan_id`，并交叉验证所有绑定身份与资产。证据缺失或歧义时明确失败，不从文件名猜测历史归属。

产品合同为预构建产品 → `e2e prepare` → `<suite>.<case>.sh` → 共享公开入口 `e2e run`。完整文件名就是用例 ID，
首段只能是 `basic`、`storage`、`image`、`network`、`sandbox`、`snapshot`、`orchestrator`、`builder` 或 `telemetry`。
内部 CI shard 按 suite 分组精确文件，plan 在执行前记录所选用例和架构排除项。

source/helper build 生成目标架构的 zot、versitygw、custom Proxy、telemetry probe、sandboxer usage probe，
以及静态无 libc 的 x86_64/aarch64 cgroup probe。 orchestrator 辅助程序集合还包含 `node-ctl-runner-test`，使用精确的 orchestrator 测试 pin 及所选依赖工作区，通过 `go test -c ./cmd/node-ctl` 编译。它仅用于测试，不是生产制品。prepare 校验架构、源码 pin 和摘要，并将 `NODE_CTL_RUNNER_TEST_BINARY` 设置为不可变的 `fixtures/bin/node-ctl-runner-test` 路径；prepare 和 E2E 均不编译它。发布包携带两种架构的 helper、精确测试 pin 和摘要。同一更早的 build 阶段按 `test/demo/requirements.lock`
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

完整源码门禁在原生 x86 的独立 Workbench system 实例中运行, 包括混合 unit/race/vet 与真实特权检查所需的编译器. 受信宿主只获取准入 `plan.test_revisions`, 实例接收 `/src` 私有副本, 不接收宿主 Docker socket、凭据或 ordinary 构建缓存. 每个精确测试 pin 的原 owner 脚本与 Make target 继续决定实际检查, 包括已发布的旧布局. 它们在实例内以 ordinary UID 执行, 保留不可读文件及拒绝提权的检查. 私有 sudo 配置保留原脚本的显式升权, 仅既有 TAP/netns 性能 fixture 通过 `sudo make test-perf-tools` 执行. 该用户仅访问实例自己的 Docker socket, 不修改宿主账号或策略. 真实 `sudo`、systemd、BPF、mount namespace、UFFD 与私有 Docker 保留原参数和断言. 门禁开始前必须成功创建 TAP 与 network namespace. 结果保留 framework/test/image 身份、各命令、退出码及耗时, 失败证据经所属实例清理入口收集. 产品、独立 helper 与发布构建使用 ordinary UID 的 build 模式. 宿主 bootstrap 保留编排及产品执行前提, Workbench producer 继续直接在原生 Runner 构建.

CI 启动已选 prepared case 时直接提供 root 权限和可信工具路径。跨 `sudo` 只传递显式准备的输入（包括私有状态目录）；执行不读取生成的 owner-runner registry。

所有非空 lane 都在没有 Go、Rust、C/C++ 编译器及组件源码树的运行时容器中 prepare。完整 storage 和 snapshot 套件也在该容器中执行，
覆盖非 KVM 和 KVM 合同；适用的原生 ARM image 用例使用相同边界。需要 host systemd 的用例保留原生 host job。
可信预检和结果记录绑定不可变运行时镜像 ID，以及编译器/源码树缺失的验证证据；零用例静态 lane 不算产品验收。
容器输入只读，可变用例目录和输出目录独立。

`snapshot.read-recovery.sh` 失败时，在 cleanup 前将用例、阶段、失败行及有界固定词表错误证据保留在 job 日志中。诊断保留测试的原始退出状态。

## 4. Daily 与 Stable

`exact-assets` 仅接受实际公开的平台 aggregate workflow。
plan 固定提交清单与暂存的十四项资产（platform + 十二个组件架构包 + SHA256SUMS），不选择产品或 helper 重建。
各 target 与 PR 共用 download/compose/公开 prepare/公开 run/result 原语，并直接消费精确暂存 platform archive 中的预构建 helper 包。
必需源码检查始终与 artifact E2E 分离。

publish 再次核对双架构成功结果与暂存摘要，原样发布归档，不重新编译。
许可证/材料、源码身份、受信任 publisher 标记及版本不可变约束继续适用。
ARM 非 KVM 范围在执行前和 aggregate 验证绑定中声明，不等同完整 VM 验收。

## 5. Native cache

`ci/native-cache/native-cache.sh restore-or-build` 处理:

- guest `vmlinux`;
- `mkfs.erofs` 与 `fsck.erofs`;
- guest `envd`;
- RocksDB headers 与 `librocksdb.a`;
- patched `cloud-hypervisor`。

缓存路径为 `$KUASAR_NATIVE_CACHE_ROOT/v3/<arch>/<component>/<input-hash>/`. Workbench action 通过 Actions cache 传输 native、Go modules/build cache、Cargo registry/Git/material 下载, 实际操作受调用者的 runtime cache 权限限制. key 区分架构、精确候选来源和声明的构建覆盖范围. candidate namespace 绑定实际 PR、companion、产品、测试/helper 与内核输入. 精确 key 和 restore 前缀都区分 source/helper 子集、已选择的产品集合及完整 manifest, 防止不可变的部分缓存占用完整构建的 key.

启用缓存的 Workbench action 在恢复后、owner 命令前运行 `go clean -testcache`. 它仅使旧 Go 测试结果过期, 保留编译和模块缓存, 避免恢复的成功结果替代当前 Job 的测试执行. Workbench receipt 记录该命令及退出码; 失败会阻止 owner 执行, 原有清理仍会运行.

Actions cache 的存储和访问属于调用者仓库及 ref, 遵守 GitHub 的缓存规则; 同 key 不会跨仓共享. 默认分支 dispatch 的候选输入仍属于 candidate. trusted cache 写入要求在源码执行前确认干净、精确的公开源码提交属于对应 main 历史, 结论保存在候选挂载之外. 现有受信 main/release 入口在 runtime cache scope 可写时提供写入路径, 沿用现有权限. publisher 可执行文件不消费候选或构建缓存.

聚合 helper 按独立选择的测试 revision 获取干净 Git 源码. Runtime 发布在安装已验证的预编译资产前冻结同一宿主 receipt, 避免把生成的二进制和 notice 当成源码改动. 原有资产验证及实际工具链/recipe 缓存 key 继续适用.

组件 `pull_request_target` job 的 cache 权限为只读. save 尝试可能报告 `cache write denied: token has no writable scopes`, 同时 cache action step 本身仍成功; 这不代表缓存保存成功. Workbench receipt 记录请求的缓存身份及 restore 结果, 不证明已写入. 缓存验收必须核对目标仓库/ref 中真实成功的 save, 再由全新 Job 恢复并验证确切产品和打包材料. 其他仓库或事件下的可写缓存不能替代这条路径的证据.

native key 覆盖 recipe、patch/config、上游内容、架构、实际工具链/ABI 和有效编译参数. 完整 Workbench/image/framework 身份保留在 provenance, 每日镜像标签不单独导致 miss. synthetic import/kernel 默认用户、主机和日期在实际构建时规范化, 显式覆盖仍有效. 固定容器路径保留 Cargo 源码和 linker 身份; 随机任务目录不进入 Actions cache path version. 写入沿用校验和与原子 rename; restore 再核验 descriptor/payload. 正常 miss 按原 recipe 构建, 匹配但损坏的条目失败, 不静默重编或原地修补.

EROFS key 包含 Libgcrypt/Libgpg-error/uuid 的 pkg-config 元数据、目标编译器/工具字节、实际本地源码归档字节（固定 URL 则使用预期摘要）及有界的编译/静态链接探针。探针跟踪实际包含的头文件（含强制 include）以及通过选项、sysroot 和库搜索路径真正选中的静态库/启动对象。源码 URL 或文件名是定位信息，不是内容身份。工作区文件使用可迁移的逻辑标签；具有语义的编译器和 sysroot 选项值仍然有效。未固定摘要的 URL 不能授权共享缓存；须使用固定 URL 或本地归档。

可选的 `guest-runtime/native-deps/deps/erofs-patches` 材料、有序 `series` 和 `deps/erofs-recipe.sh` 都进入 key。仍支持不含这些文件的旧源码集合，包括旧 OpenSSL 配方的实际目标链接探针。新增、修改或移除输入都会使 key 失效。Workbench 镜像安装 `libgcrypt20-dev libgpg-error-dev uuid-dev`，并保留 `libssl-dev` 以支持已经准入的旧源码集合。openEuler 24.03-LTS-SP4 的 `libgcrypt-1.10.2-4` 和 `libgpg-error-1.47-1` 源码 RPM 明确禁用静态库，仅安装 devel 软件包不够。[Runner provider](../ci/runner/README_zh.md#安装) 以最多两个 job 构建这些 pin 且包含发行版补丁的源码,仅安装静态 archive 及经过验证的源码/构建/重新链接/许可目录,并验证热复用和模板到 slot 的复制。Runtime 打包验证相同的 pin 目录;Ubuntu 保留已安装软件包材料路径。

EROFS 条目必须包含匹配的 `.erofs-recipe` stamp、二进制摘要、源码归档/源码树、外部链接依赖、maps、对象、许可证及 relink 输入. Envd 保留匹配的 Go workspace 源码上下文. Cloud Hypervisor 保留 patched 源树、原始 build report、linker map、Cargo lock/metadata/source manifests 和许可证; Cargo registry/Git 与已验证的 release-material 下载随 native 条目配套恢复. 恢复后的 package 必须使用这些确切材料通过 validate, 不替换旧证据或静默重编. 材料不完整的旧 schema 产生 miss. 仓库补丁仍来自准入 source set, 独立打包验证器保留访问精确 Git 对象的能力.

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
ARM 当前只选择 accelerator 和 guest-runtime 已有独立非 KVM 套件；其余 owner 明确未选 E2E。
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
