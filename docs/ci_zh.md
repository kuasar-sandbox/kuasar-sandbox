[English](ci.md) | [简体中文](ci_zh.md)

# 持续集成

上线分为两个受保护步骤：先合入共享构建/发布原语，新聚合制品通过 `integration-artifacts.yml` 验证。初始化首个基线期间，PR 沿用公开标准 runner 上的既有源码流程，必需源码检查和 fixture 编译位于 E2E 之前，候选执行 job 不接收 App 私钥。配套组件变更和首个双架构聚合通过既定 profile 后，由 PR #129 切换默认 PR 入口并删除临时 workflow 别名。这是显式上线阶段；制品基线缺失时不会自动回退源码构建。

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

`pull_request_target` 仍只接受 `main` 和 `release/vMAJOR.MINOR.x`。非 draft 同仓 PR 自动准入；
fork 准入仍查询当前 active 组织成员身份，不能以 `author_association` 代替。
admission 复核当前 PR/base/head 和 integration commit 的两个父提交，并向该 integration commit
写入 `kuasar/ci-exact-head=pending`。draft、外部 fork 和冲突都不能得到 E2E 成功结论。

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
不要求任意旧编排源码重建与基线天然字节相同。

## 3. 两条独立架构生命周期

```text
resolve → x86 build → x86 prepare → 原生 x86 shards → x86 result
        → ARM cross build → ARM prepare → 原生 ARM shards → ARM result
        → 独立源码 unit/race/vet 与 UFFD 检查
                                  全部所选结果显式汇总
```

两种产品都在独立 `ubuntu-24.04` x86 job/workspace 使用已有 Makefile/native-cache 构建。
每架构产品构建一次、不变输入 prepare 一次。一条 lane 自己准备完成即可开始 E2E，不等待另一架构构建。
执行使用 `ubuntu-24.04` 或标准 `ubuntu-24.04-arm`；concurrency、artifact、结果均包含架构/shard/run。
逐个汇总所有 shard 和两种架构，避免 matrix output 覆盖一边。

RocksDB 与 cache-ctl 共用显式 `CROSS_PREFIX`、目标 CGO CC/CXX；Rust 使用环境编译器及其匹配 target std/linker。
ARM kernel 校验 `Image` 头；Go/native ELF 必须为目标 Linux ELF64。
EROFS 使用目标静态依赖与目标 pkg-config；host `BUILD_MKFS_EROFS`、Runtime writer/readers、Go 打包 helper 保持 host-native。
不使用 `NO_ROCKSDB`，也不建立环境 Go/Rust 二进制字节白名单。

prepare 在组装前校验包路径/类型/权限/归属、摘要、必要产品和 runtime 内嵌身份。
两个架构独立解压；候选测试 owner 整棵目录替换，包括 helper 和删除文件残留检查。
其余测试来自所选 platform 包，按 owner 记录 test revision，与可信 framework SHA 分开。

prepare 提供 manifest Docker archive、guest flatten fixture、固定 image ID/digest、orchestrator 基础镜像以及目标 helper
（zot、versitygw、custom Proxy、telemetry probe）。工作区包含 `bin/`、`test/`、`fixtures/`、`images/`、材料及 `provenance.json`。
这些是测试输入；被测业务 Build、flatten、snapshot、publish、restore 仍在原有 E2E 用例执行。

E2E 只 checkout 可信执行器并下载目标 prepared workspace，执行前后核验全部文件摘要和权限。
它不 checkout 组件源码，不隐式进行产品 Go/Cargo/kernel 编译。
每个 shard 使用短路径、磁盘支持的私有可变目录，socket、direct I/O、Docker 配置、性能状态均在不可变输入之外。
源码依赖的 sandboxer/orchestrator unit/race/vet、真实 ENOSPC、Collector 回归和 UFFD benchmark 保留为独立必需源码 job。
source 模式 x86 sandboxer/platform 还用同一组制品保留 A/B/C/D `off/auto × cold/warm` working-set smoke。

## 4. Daily 与 Stable

`exact-assets` 仅接受实际公开的平台 aggregate workflow。
plan 固定提交清单与暂存的十四项资产（platform + 十二个组件架构包 + SHA256SUMS），不选择产品重建。
各 target 与 PR 共用 download/compose/prepare/profile/result 原语；测试 helper 可从所选精确测试源码预先构建，
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

缓存路径为 `$KUASAR_NATIVE_CACHE_ROOT/v2/<arch>/<component>/<input-hash>/`。hosted
新版公开 workflow 将根目录设在每个一次性 build job 的临时目录内。
hosted 只做本地复用,不向 Actions cache 或 artifact 上传缓存。input hash 覆盖
构建脚本、patch/config、上游摘要、架构、Go/Cargo/C/C++ 工具链和 pkg-config 解析结果。
条目通过 staging、校验和及原子 rename 发布;命中恢复前重新校验 descriptor、payload 和
tar 路径。损坏条目失败,不会在原目录修补。

EROFS key 包含 Libgcrypt/Libgpg-error/uuid 的 pkg-config 元数据、目标编译器/工具字节、实际本地源码归档字节（固定 URL 则使用预期摘要）及有界的编译/静态链接探针。探针跟踪实际包含的头文件（含强制 include）以及通过选项、sysroot 和库搜索路径真正选中的静态库/启动对象。源码 URL 或文件名是定位信息，不是内容身份。工作区文件使用可迁移的逻辑标签；具有语义的编译器和 sysroot 选项值仍然有效。未固定摘要的 URL 不能授权共享缓存；须使用固定 URL 或本地归档。

可选的 `guest-runtime/native-deps/deps/erofs-patches` 材料、有序 `series` 和 `deps/erofs-recipe.sh` 都进入 key。仍支持不含这些文件的旧源码集合，包括旧 OpenSSL 配方的实际目标链接探针。新增、修改或移除输入都会使 key 失效。hosted native profile 安装 `libgcrypt20-dev libgpg-error-dev uuid-dev`，并保留 `libssl-dev` 以支持已经准入的旧源码集合。openEuler 24.03-LTS-SP4 的 `libgcrypt-1.10.2-4` 和 `libgpg-error-1.47-1` 源码 RPM 明确禁用静态库，仅安装 devel 软件包不够。[Runner provider](../ci/runner/README_zh.md#安装) 以最多两个 job 构建这些 pin 且包含发行版补丁的源码,仅安装静态 archive 及经过验证的源码/构建/重新链接/许可目录,并验证热复用和模板到 slot 的复制。Runtime 打包验证相同的 pin 目录;Ubuntu 保留已安装软件包材料路径。

EROFS 保留唯一可选的 `bin/<arch>/.erofs-recipe` v2 stamp（含两个输出摘要和实际外部编译/链接依赖）、两个链接映射及其 EROFS 对象/静态库输入，以及源码 `LICENSES`、`AUTHORS` 和 `COPYING`。恢复相同配方时无需完整解压源码树即可复用。没有 stamp 的旧缓存仍可读取，并在下一次配方检查时重建。仓库补丁文件仍来自准入的 source set，cache restore 不覆盖它们。Runtime 补丁材料验证使用所选提交的本地 Git 对象；独立验证器必须能访问这些对象。真实发布打包仍须配齐实际目标的版权/声明及源码/重新链接输入。

同 key 构建和恢复持有条目锁。每组件默认保留最近使用的 4 个 key,且只回收超过保护期并能
非阻塞取得锁的条目。缓存测试入口:

```bash
make -C kuasar-sandbox test-ci-tools
```

## 6. Hosted 前置条件与证据

bootstrap 保留环境 Go/Rust 版本，仅在一次性 x86 job 添加目标包和匹配 Rust target，不升级编译器或安装 runner 服务。
host EROFS readers 与 Runtime reader 复用已有固定 recipe。
源码、native 和编译器缓存留在各 job，不上传。

`artifact-build`/`artifact-cross` 提供原生/交叉构建条件；`artifact-prepare` 提供 host readers 与 fixture 工具；
`artifact-x86` 提供 Docker/systemd/cgroup v2/KVM/UFFD/netns/BPF；`artifact-arm` 仅提供所选非 KVM 条件。
`source` 保留源码 benchmark 条件。x86 VM bootstrap 保留既有窄范围每 job KVM udev/group 修复，
检查真实非特权 KVM/UFFD 访问，TUN/vhost-vsock ACL 保持原有方式。缺少所选能力必须失败。
CPU affinity 和 Go/Cargo 并发由可用 CPU/内存限制。

证据 artifact 包括 `integration-plan`、每架构 `integration-provenance`、每 shard `integration-shard`、
`integration-source-result`、两个 `integration-architecture-result` 和最终 `integration-validation`，均带 run ID/attempt。
provenance 记录基线资产、产品来源/hash、精确源码/测试/框架、内嵌载荷、实际工具/native key、helper/fixture hash、权限及既定 profile。
结果记录所选用例、退出码、耗时和 prepared provenance 摘要。
聚合 cleanup 仅移除大型 build/prepared/stage 传输物，验证元数据保留七天；不上传含测试凭据的原始运行状态。

轻量合同检查沿用 `make test-ci-tools test-release-tools test-perf-tools`，需可信 EROFS readers 与 `KUASAR_RUNTIME_READER`。
组件 release/workflow/fixture 检查保留原入口。开发者 `make test-e2e` 仍先准备源码产品/helper，再运行 owner 套件，
与 hosted artifact executor 分开。合同 fixture 和原生预检不能代替实际公开标准 runner 验收。

## 7. 首轮覆盖与切换

架构/owner/用例、缺失原因、已执行证据和后续事项集中在唯一的 [首轮覆盖表](ci.md#7-initial-coverage-and-rollout-evidence)。
ARM 当前只选择 accelerator 和 guest-runtime 已有独立非 KVM 套件；其余 owner 明确未选 E2E。
ARM VM/KVM/restore/Builder/cluster 同等覆盖、新硬件和全面用例扩充属于后续工作。
已选测试失败不能事后改为 unsupported，整个 ARM job 不使用 continue-on-error。

四组件 Public 切换复用 #82 准备记录与 #128 迁移工作，只复核新增差异和真实凭据/分发阻塞。
读回 actual visibility 后执行实际 caller CI。私有 guard 导致的未运行不算成功；保护与评审按正常规则执行。
历史测试访问能力的销毁/未复用事实仍需确认，代码预检不能代替该证据。

## 8. 参阅

- [发布规约](release_zh.md)：版本选择、ARM 初始化与资产发布。
- [部署](deployment_zh.md)：运行能力与服务。
- [Runner 运维](../ci/runner/README.md)：既有持久基础设施。
- [测试快速开始](../test/QUICKSTART.md)：开发者 E2E 条件。
