[English](release.md) | [简体中文](release_zh.md)

# Release

## 1. 概述

Kuasar Sandbox 将组件发布与平台聚合发布分开。组件版本描述一个组件仓的独立交付,
聚合版本描述一组已经发布并共同通过精确资产验证的组件。平台聚合版本与组件版本互不
推导,也不要求同名。

发布单元为:

- `accelerator`、`connector`、`sandboxer`、`orchestrator`: `vX.Y.Z`;
- `guest-runtime` runtime: `runtime-vX.Y.Z`;
- `guest-runtime` kernel: `vmlinux-vX.Y.Z`;
- 平台聚合: `release-vX.Y.Z`。

`guest-runtime` 有 runtime 和 vmlinux 两个发布单元,但仍然是一个组件仓。所有版本线都
接受 `-preview.YYYYMMDD[.N]` 后缀。可选修订号 `N` 是不含前导零的十进制正整数;
例如 `preview.20260914.1` 表示该日期的第一次显式修订。日期和修订号按数值排序,
次日排在前一日期的所有修订之后。Preview 是 GitHub prerelease,Stable 是非 prerelease;
任何已发布版本都不覆盖、不改名、不重建。

## 2. 配置规约

每个受维护的平台分支恰好使用两个清单:

- `releases/release.yaml`:该分支计划发布的下一个 Stable 聚合版本;
- `releases/daily-preview.yaml`:该分支当前维护的 Daily Preview 版本。

这是正式发布状态规约,不增加第二套版本元数据或历史目录。旧选择由清单的 first-parent
Git 历史保存。解析器始终要求:

```text
daily-preview.yaml/version >= release.yaml/version
```

比较对象是数值化的聚合 `MAJOR.MINOR.PATCH`,不是字符串。Stable 清单只能选择 Stable
组件;Preview 清单可以混合选择 Stable 和 Preview 组件。两个清单都必须精确列出六个发布
单元,并使用各自正确的 Tag 前缀。

下面两个清单都是示例,不是当前已发布版本的清单。

### 2.1 Stable 清单

首个 Stable 可省略 `previous_version`;提供时必须指向更早的 Stable。
首个 Stable 的发布说明不以 Preview 或整个仓库提交历史代替比较基线。

```yaml
version: release-v0.5.7
previous_version: release-v0.5.6
components:
  accelerator: v0.2.1
  connector: v0.1.9
  sandboxer: v0.3.5
  orchestrator: v0.4.3
  runtime: runtime-v0.1.2
  vmlinux: vmlinux-v0.1.0
```

`previous_version` 必须严格早于 `version`。正式聚合可以选择彼此不同、也与聚合版本不同
的组件版本。

### 2.2 Daily Preview 清单

```yaml
version: release-v0.5.7
previous_version: release-v0.5.6
preview_version: preview.20260831
previous_preview_version: preview.20260830
components:
  accelerator: v0.2.2-preview.20260831
  connector: v0.1.9
  sandboxer: v0.3.6-preview.20260831
  orchestrator: v0.4.3
  runtime: runtime-v0.1.2
  vmlinux: vmlinux-v0.1.0
```

完整聚合 Tag 是 `version + "-" + preview_version`。同一聚合版本线的后续 Preview 以
`previous_preview_version` 为更新基线;该版本线的首个 Preview 以 `previous_version`
为基线。

新聚合的同一份 Stable 或 Daily 清单还必须固定五个独立测试 revision（将占位符替换为完整小写提交 SHA）：

```yaml
test_revisions:
  accelerator: <accelerator-test-sha>
  connector: <connector-test-sha>
  guest-runtime: <guest-runtime-test-sha>
  sandboxer: <sandboxer-test-sha>
  orchestrator: <orchestrator-test-sha>
```

Daily 写入可信计划已经解析的组件源码精确 HEAD，即使产品 unit 被复用。维护选择缺少组件源码分支时，必须已有明确提交的测试 pin。
准备新 Stable 选择时应提交明确的 pin；
平台测试使用聚合源码 SHA。platform 包从各精确 test pin 组装扁平用例和按 owner 命名的库，并携带双架构预构建 helper；组件文档使用相同 test pin，确保调用说明修正与用例一起交付。产品保留所选 unit 版本，kernel 文档仍使用独立选择的 vmlinux 源码。
更早的 helper build 还按提交的版本/摘要 lock 获取 Python 3.12 完整 Demo wheel 依赖闭包。打包要求两种架构的本地 wheel 和包名/版本/摘要 manifest；公开 prepare 仅从这些 wheel 离线安装，不使用 index。
helper 编译、prepared 输入、结果和现有发布验证 binding 保留相同 pin，后续 baseline 对照提交清单核验。
暂存的 `test-revisions.json` 仅供内部验证，公开资产名称和产品字节不变。新打包缺失或错配 pin 时失败；
历史清单和 Release 继续按原契约读取，不补写或修改。

只有当前聚合已完整发布后,才推进 `previous_preview_version`。未发布的选择跨日滚动时,
保留其已有基线;若是该版本线的首个 Preview,则继续省略此字段。被放弃的选择可能包含
从未发布的组件 Tag,不能作为更新比较基线。

Stable `V1` 发布后,如果继续在同一分支开发,必须先将 Daily 清单推进为 `V2`,并设置
`previous_version: V1`。准备 `V2` 时再推进 Stable 清单。`V2` 发布后两个清单继续推进
到 `V3`,以此类推。Daily 与 Stable 清单版本相等且该 Stable 聚合已经发布时,该版本线
关闭:禁止新建 Preview,也禁止重复发布 Stable。若同版本 Stable Release 已出现但资产或
Tag 尚不完整,Daily 同样在任何清单或组件变更前延后,不在残缺 Stable 旁继续发布 Preview。

## 3. 分支与版本线

平台 `main` 发布最新主线版本。维护补丁使用平台 `release/vMAJOR.MINOR.x`。例如主线发布
`release-v0.1.0` 后,可以从该 Tag 建立 `release/v0.1.x`;维护分支发布
`release-v0.1.1`、`release-v0.1.2`,主线随后可以发布 `release-v0.2.0`。

若 `release-v0.2.0` 尚未规划而 `release-v0.1.0` 立即需要补丁,允许先在 `main` 发布 `release-v0.1.1`、
`release-v0.1.2`,再从较晚的补丁点建立 `release/v0.1.x`。建立维护分支之后,主线与维护线各自
维护自己的两个清单。

平台仓的 GitHub Latest 只由平台 `main` 的 Stable 聚合发布更新。每个组件仓仍独立维护
自己的 Latest:组件 `main` 的 Stable 发布记录源码分支和精确提交;独立的幂等协调工作流
在该仓所有已发布的主线 Stable 中按源码提交先后重新选择 Latest,同一提交存在多个 Tag
时才比较 SemVer。协调工作流由任一组件发布完成触发,跨版本串行,失败可独立重跑,并有
定时自愈。组件维护分支 Stable 和任何 Preview 不更新 Latest。平台聚合始终通过清单中的
精确 Tag 选择组件,不依赖组件仓的 Latest。

平台维护分支与组件维护分支不存在同名约束。平台 `release/v0.5.x` 可以聚合
`sandboxer release/v0.3.x`、`orchestrator release/v0.4.x` 和只在 `main` 发布的
vmlinux 固定版本。

## 4. Daily 分支扫描与组件选择

`Daily Preview Scanner` 每天按 Asia/Shanghai 时区定时扫描:

- 平台 `main`;
- 所有符合 `release/vMAJOR.MINOR.x` 的平台分支。

scanner 固定每个分支的 HEAD SHA,再从平台 `main` 加载受信任的控制器处理该 SHA。
控制器使用 GitHub App 的只读 contents Token 访问组件仓;Token 只通过子进程级 Git
HTTP authorization header 传递,不写入 remote URL、仓库配置或日志。
所有分支协调器与 Preview GC 共用一个 GitHub Actions concurrency key,因此清单选择和
GC 计划/派发不会重叠。scanner 按分支顺序派发并等待;被取消或超时的分支任务在本轮最多
重试三次,仍未完成则由下一次扫描恢复。分支在选择或提交清单期间移动时,本次不覆盖远端
状态,下一次从新 HEAD 重扫。某个分支确定性失败时,scanner 记录失败但继续处理其余分支,
最后统一返回失败,避免一条损坏的维护线长期阻塞其他分支。

### 4.1 源分支映射

- 平台 `main` 总是选择每个组件仓的 `main`;
- 平台维护分支从 Daily 清单中该 unit 的版本派生组件分支。`v0.3.5`、
  `v0.3.6-preview.*` 都映射到组件 `release/v0.3.x`;
- runtime 与 vmlinux 分别从 `runtime-vX.Y.Z`、`vmlinux-vX.Y.Z` 派生
  `release/vX.Y.x`;
- 派生分支不存在时,该 unit 固定复用清单指定的完整 Release,不自动修改版本。
  如果上游依赖在本轮改变而该 unit 必须重建,则整次选择 deferred,不会把旧下游二进制与
  新依赖版本写进同一个聚合清单。

因此同一个平台维护分支中的六个 unit 可以来自六条彼此不同的组件版本线。

### 4.2 Tag 选择

Tag 选择不在整个分支历史上比较最大 SemVer。算法从所选分支 HEAD 沿 first-parent 向后
扫描,第一个带有完整可用 Release Tag 的提交获胜。仅当同一个提交存在多个合法 Tag 时,
才以 SemVer 选择最大者。维护分支还会忽略不属于其 `MAJOR.MINOR` 的 Tag。

Daily 清单中指定的 Tag 也参加提交位置比较:

- 位于更近 HEAD 的提交者获胜;
- 位于同一提交时取最大 SemVer;
- 不在所选分支 first-parent 上时停止,不把旁支或历史大版本误选进来。

当 unit 实际产品/打包/材料输入未变化时，复用获胜 Tag，即使文档、测试或 workflow 已推进 HEAD。
kernel 继续使用已有 Makefile 相关输入投影。相关输入变化后，Stable winner 的 Patch 自增一次形成
`vX.Y.(Z+1)-preview.YYYYMMDD`；Preview winner 保持 core，仅改变日期/显式修订号。

依赖选择比较 consumer 实际链接/内嵌的源码输入。独立 CLI 或文档变化不会无条件重建全部反向依赖。
runtime 跟踪 accelerator flatten 库及 sandbox-init 输入；其他 Go consumer 跟踪链接库。
复用的 Preview 保留原依赖版本绑定；若依赖版本元组不同，必须证明相关源码输入一致，不能跳过来源校验。
vmlinux 不因这些依赖变化重建。同日同修订号已发布后再次需要重建时保持 deferred；可显式选择更大的修订号，
例如 `gh workflow run daily-preview.yml --ref main -f date=20260914.1`。
重复请求恢复相同发布，不覆盖完整 Release，不自动递增修订号。
新发布组件与聚合使用相同日期/修订后缀，复用版本保留原精确来源。

平台自身在所选分支上的代码变化同样会产生新的聚合 Preview,即使六个组件都复用。
同一聚合版本的 workflow run 名同时绑定平台源码 SHA。只要任意匹配 run 仍在运行,即使
更新的 run 已取消或结束,控制器也保持当前 Daily 清单不变;分支产生新 HEAD 后使用新的
run 身份,不会用旧输入重跑或改写运行中清单。
分支协调任务的身份还包含请求日期,因此相同平台 SHA 的不同日期扫描不会错误复用彼此的
结果。

若依赖重建已写入 Daily 清单,但对应组件 Release 尚未发布,后续扫描会继续完成这个
待发布 Preview,即使所选依赖已与清单一致。获胜 Tag 为 Stable 或 Preview 时均适用;
日期或修订号推进后,使用新选择的后缀发布重建结果。完整 Release 仍须通过原有的源码和
依赖绑定校验。

<a id="first-arm-initialization"></a>
### 首次 ARM 初始化

主线初始化已完成。[release-v0.1.5-preview.20260922.4](https://github.com/kuasar-sandbox/kuasar-sandbox/releases/tag/release-v0.1.5-preview.20260922.4)
是首个通过 x86 与既定原生 ARM 非 KVM profile 的已发布基线；
[聚合 run 35751885794](https://github.com/kuasar-sandbox/kuasar-sandbox/actions/runs/35751885794)
将结果绑定到源码/框架 `69c26d2e9d7a494b3463e42c3b8c20ac1119de4f`，十四项资产保持实际测试的字节。
随后 [PR #129](https://github.com/kuasar-sandbox/kuasar-sandbox/pull/129)
在 `dea0bc66ae766caf47f3c6684a7f426b79b730ba` 启用普通 artifact PR 基线解析。
普通 PR/Daily 使用无需再次初始化或重跑该发布；`initialize_arm` 默认仍为 false。
声明的 profile 与保留证据见 [CI 覆盖表](ci.md#7-initial-coverage-and-rollout-evidence)。

公开目标实现与四组件授权切换完成后，显式派发 `daily-preview-branch.yml`，使用已有的精确
`platform_ref`、`platform_sha`、`date` 输入，并设 `initialize_arm=true`。`INITIALIZE_ARM` 默认 false。
选择合法的新 Preview 日期/修订号；历史 AMD64-only unit 使用新版本，从同一精确源码/依赖元组构建两个 target。
缺少维护源码分支时延后，不虚构替代版本。

聚合只要求当前声明的 x86 与 ARM 原生非 KVM profile，不等待 ARM KVM 同等覆盖。
发布后普通 PR/Daily 使用该精确基线；不会修改历史 tag/资产/SHA256SUMS，不隐式初始化或回退全源码构建。
新 Stable 同样需要选择双架构 unit 的新版本，历史 Stable 保持原样。

## 5. 残缺发布恢复

完整组件 Release 必须非 draft、prerelease 状态与 Tag 一致，且精确约定的归档与 `SHA256SUMS` 都已 uploaded。
历史 AMD64-only 组件/聚合保留两项/八项契约，双架构组件使用三项，历史双架构聚合保留十四项；新 `workbench-v1` 聚合必须具备全部十六项资产。ARM 集合残缺仍是残缺；
旧 AMD64-only 发布不因初始化被重判为损坏或删除。

选择器仅把完整 Release 的 Tag 当作候选。残缺状态不让整条 Daily schedule 失败:

- 匹配的发布 run 仍在 queued/in progress:延后并在后续扫描恢复;
- 当前 Daily 清单拥有的残缺 Preview:先调用组件本仓的删除工作流,核对精确源码 SHA 后
  删除 Release、资产和 Tag,再重扫并重新走正常 Daily 发布;
- 外来残缺 Preview 或残缺 Stable:忽略,不作为候选,也不自动删除;
- 无组件维护分支的固定版本不完整:延后,不派生替代版本;
- 确定性失败最多尝试三次,超过后分支协调器和扫描器均报告失败,不会靠覆盖 Tag 或手工拼资产恢复。组件
  发布和删除的普通 failure 只重跑失败 job;cancelled、timed out 等没有失败 job 的状态
  重跑完整 workflow。聚合构建/验证失败创建同一精确输入的新 workflow run，重新拉取组件 Release 并生成新的 exact stage。
  对同一源码的残缺 `workbench-v1` 发布，控制器先核实原 prepare/stage/validation 成功且不可变 stage 仍保留，
  然后只重跑失败的 publish job，不重建镜像或重跑健康验证；stage 缺失或重试耗尽时失败关闭；三次预算按这些 run 的
  `run_attempt` 总数累计。

若未完成的聚合选择推进到新的上海日期或显式请求的更大修订号,控制器不再用旧日期 Tag 绑定新的组件 HEAD。
残缺聚合对象先通过受保护入口删除;随后清单滚到新日期并重新选择。旧选择中已经完整发布
但未被任何保留聚合引用的组件 Preview 交给 GC 按回退窗口统一删除。若未请求更大修订号且未跨日,控制器
保持 deferred。带修订号的 Preview 使用同样的受保护恢复和 GC 规则:删除仍受 Stable 关闭
版本线后的七天回退窗口、规范归属和保留引用约束。

恢复的目标是恢复 Daily 流程,不是修补或重建残缺 Release 的资产。同名完整 Release 永远
不会被 `incomplete` 模式删除。若 Tag 已经缺失但 draft 或 prerelease 仍在,只有
Release 的 `target_commitish` 是完整且匹配的源码 SHA 时才允许恢复删除。若同名残缺对象
在一次成功清理后再次出现,控制器创建新的清理 run,不把历史成功结果当作当前对象已收敛。

## 6. 组件发布 CLI

组件 workflow 始终从仓库 `main` 加载受信任工具,但必须显式传入实际源码分支和精确
分支 HEAD SHA。下面只展示参数形态;自动 Daily 由控制器填写这些值:

```bash
gh workflow run release.yml \
  --repo kuasar-sandbox/accelerator --ref main \
  -f version=v0.2.2-preview.20260831 \
  -f source_ref=release/v0.2.x -f source_sha=<full-sha> \
  -f aggregate_version=release-v0.5.7-preview.20260831 \
  -f aggregate_sha=<platform-manifest-commit>

gh workflow run release.yml \
  --repo kuasar-sandbox/sandboxer --ref main \
  -f version=v0.3.6-preview.20260831 \
  -f source_ref=release/v0.3.x -f source_sha=<full-sha> \
  -f aggregate_version=release-v0.5.7-preview.20260831 \
  -f aggregate_sha=<platform-manifest-commit> \
  -f accelerator_version=v0.2.2-preview.20260831 \
  -f connector_version=v0.1.9
```

orchestrator 接收 `accelerator_version`、`connector_version` 和 `sandboxer_version`;
runtime 只接收与镜像实际载荷一致的 `accelerator_version` 和 `sandboxer_version`;
vmlinux 不接收内部组件版本输入。runtime 与 vmlinux 使用各自工作流。
预检要求 `source_sha` 仍是 `source_ref` 的 HEAD,构建 checkout 该 SHA,最终 Tag 也指向该
SHA。依赖参数必须对应已经完整发布的精确 Release。
Preview 还要求 `aggregate_sha` 所指平台提交中的
`daily-preview.yaml/version + preview_version` 精确等于聚合版本,且
`components.<unit>` 精确等于待发布 Tag。该绑定和 Stable 封线检查在构建前及取得
publish concurrency lock 后各执行一次。所有组件 Release notes 都记录源码分支、源码
SHA 和发布单元。Preview 还记录原始聚合清单 SHA 和实际依赖版本绑定;已有 Preview 只有
在源码与依赖绑定都一致时才可复用。

workflow run name 包含源码 SHA 和依赖版本元组。控制器只重跑相同输入元组的失败 run;
分支 HEAD 或依赖选择已经改变时创建新的 dispatch,不会用旧输入消耗三次恢复机会。

## 7. 聚合发布 CLI

要收敛已在所选分支提交配置的聚合版本,本地发布入口先处理所需组件单元,再执行聚合事务:

```bash
RELEASE_VERSION=$(awk '$1 == "version:" {print $2}' \
  kuasar-sandbox/releases/release.yaml)
PLATFORM_REF=$(git -C kuasar-sandbox branch --show-current)
PLATFORM_SHA=$(git -C kuasar-sandbox rev-parse HEAD)
make -C kuasar-sandbox release RELEASE_VERSION="$RELEASE_VERSION" \
  PLATFORM_REF="$PLATFORM_REF" PLATFORM_SHA="$PLATFORM_SHA"
```


聚合工作流同样从平台 `main` 加载受信任工具,但版本选择、系统文档、平台用例和打包
内容来自所选平台分支的精确 HEAD。目标分支中的旧发布脚本不会作为控制器执行:

```bash
gh workflow run aggregate-release.yml \
  --repo kuasar-sandbox/kuasar-sandbox --ref main \
  -f version=release-v0.5.7 \
  -f source_ref=release/v0.5.x \
  -f source_sha=<full-sha>
```

正式发布前先在目标分支提交 `release.yaml`,发布所缺的 Stable 组件单元,再从目标分支最新
HEAD 触发聚合。主线 Stable 成功后成为 Latest;维护分支 Stable 保留为可发现的正式版本,
但不抢占主线 Latest。Stable 与 Preview 都必须由该 HEAD 当前清单直接选择;历史清单只
用于解析已存在 Release 和 GC,不能通过手工 dispatch 回填成新的聚合发布。

聚合 prepare 生成的短期 artifact 是一次 run 的不可变发布证据。构建/验证失败后重新 dispatch 相同版本、源码分支和源码 SHA，确保新的 prepare 重新下载当前组件 Release。
新契约的残缺发布失败后，由控制器验证原先的成功前置 job 和保留 stage，只重跑该 publish job；
不要完整重跑旧 workflow 来替换镜像字节。控制器按精确 run name 汇总新旧 run 的尝试次数,总计
三次后报告失败。

本地发布工具验证入口为:

```bash
make test-release-tools
make test-ci-tools
```

## 8. 发行资产验证

每个新组件 Release 包含 x86_64、aarch64 两个 archive 与 `SHA256SUMS`。
runtime、vmlinux 保留独立包名。两份维护中的聚合清单声明 `delivery: workbench-v1`。新 aggregate 包含架构无关的 platform 包、十二个原样组件包、两个原生 workbench Docker 镜像归档及统一 `SHA256SUMS`，共十六项资产。历史 AMD64-only 和双架构聚合保留八项/十四项原契约。reader 从精确 Tag 源码读取契约；缺失新资产绝不回退为历史兼容模式。

不发布项目生成的 release metadata JSON,也不重复上传 GitHub 自动提供的源码归档。
版本选择 YAML 只在仓库维护,既不是 Release 资产,也不进入 platform 包。
组件 archive 不携带 `docs/` 或 `test/e2e/`；aggregate assembly 从独立 test pin 收集组件文档
和 E2E 输入，统一写入 platform archive（§8.1）。kernel 文档使用单独选择的 vmlinux 产品源码。

组件原生/ARM 交叉构建在两个独立 x86 job 执行，复用相同精确源码与依赖版本，校验后原样组装双架构包。
依赖 Release 必须公开、完整并解析为轻量 tag 的精确 commit，build checkout 不保留凭据。
聚合 prepare 下载六个所选 Release，校验 API size/digest、SHA-256、路径、归属与跨包覆盖，
随后在无凭据源码步骤中构建固定测试 helper，并生成确定性的 platform 包。
暂存字节通过共享的公开 prepare → 聚焦用例 → 公开 run 合同，不重建产品或 helper。
解析阶段按已提交 test pin 声明精确用例文件名；执行使用短的私有状态路径，直接读取暂存归档中的用例，无需源码 overlay。
x86 在真实 KVM runner 运行九个 suite 中的所选用例；托管 ARM 运行预先声明的 accelerator/guest-runtime 非 KVM 子集。
干净运行时中的 prepare 和所选完整 suite 执行保留 Go、Rust、组件源码树缺失的验证证据。源码检查是独立必需 job。
publish 必须收齐两个架构的成功结果，在 `kuasar-integration-validation` 绑定用例选择、源码身份和资产摘要，原样上传归档。

Preview、维护分支 Stable 和主线 Stable 使用相同资产与发行资产验证门禁;差别只在发行状态与
Latest 策略。

<a id="平台包中的文档"></a>
Workbench 的 prepare 与构建期 collector 共享规范用例发现和外部镜像请求函数。collector 在同一暂存请求中只解析一次移动 Tag，并用原始 registry manifest/config 证据绑定所复制的 Docker archive。原生构建每种架构的单个 gzip Docker 镜像归档，标注相同聚合版本和精确源码。实际压缩字节必须**小于 2 GiB**；超限或残缺输入在暂存/发布前失败。镜像只包含工具和外部输入，六个产品归档保持独立且与上游字节一致。

新增原生 workbench 门禁导入这些精确暂存字节，从空的私有 Docker 状态、无外部路由开始，通过公开入口离线 prepare，再仅重新连接所属 bridge 执行 run，保留 Demo 真实的 Guest Internet 出站断言。具备 KVM 的原生 x86 和 ARM 使用相同的当前完整常规用例选择；GitHub 托管 ARM runner 没有 KVM 设备：其显式 `artifact-only` workbench 结果验证归档、导入镜像及原生发行输入，单独记录计划用例，不声称完成系统或离线执行。ARM 原有原生构建及非 KVM E2E 通道仍为必需。完整原生 ARM workbench 验收使用已发布字节及与 x86 相同的 `--all --exclude storage.obs.sh` 选择，包含 orchestrator、builder、telemetry 和静态无 libc cgroup probe。所选 Guest 内核必须提供实际 PMEM/EROFS `dax=always`，保留架构及运行断言。用例列表由精确发行文件产生，不固定为历史数量。旧的有限 workbench 验收仅对其原有用例范围有效。私有 daemon 隔离、实际所选 KVM/UFFD/TUN/BPF 操作及所有权清理都必须通过。Workbench 系统模式使用 Docker privileged 权限，面向可信系统管理员；自定义宿主 AppArmor/seccomp 策略工具及权限收缩专项证明不再是部署或发布前置条件。通用系统启动报告不可用的硬件能力，不冒充相关产品测试已经通过。工具链环境不能替代单独的无编译器 runtime 门禁。新的原生结果声明 `native-full`，历史 `system` 保留原有选择范围，托管 ARM 继续显式声明 `artifact-only`。这些记录绑定精确 framework、测试 pin、产品摘要、archive/config 身份、实际压缩大小、压缩/导入时间和观测到的磁盘峰值；系统结果另含启动时间及已执行用例证据；磁盘观测不是配额。

发布把已测试的保存镜像复制到 `ghcr.io/kuasar-sandbox/workbench:<去掉-release-前缀的聚合版本>`，验证两种架构的 config 身份和多架构 index，并匿名回读每个按 digest 固定的镜像到临时 Docker archive。归档校验器将每个解压后的镜像层与已测试离线镜像的 config 逐一绑定，发布重试也必须通过。registry digest、镜像 ID、离线 archive hash 含义各自独立。已有同名 Tag/资产必须匹配；重试只补传缺失资产，不覆盖测试字节。跨服务发布可恢复：只有 registry 和完整 GitHub 资产集合一致，GitHub draft 才公开。冲突时失败关闭，不改写历史源码和已发布字节。

用户只解压所选产品架构与 `platform-release`；`workbench-<arch>-v*.tar.gz` 使用 `docker load` 导入，不能解压到产品目录。[快速开始](quickstart_zh.md) 检查声明集合并按显式资产类别下载。用户可以不下载 workbench，但新聚合发布必须包含两种架构镜像。

### 8.1 文档载荷与源码映射

平台归档使用 `test/e2e/package_inputs.py` 中显式的构建期文件列表。组件输入来自
`test_revisions`，kernel 指南来自单独选择的 vmlinux 源码。组装器复制规范用例、明确列出的
运行库、Demo 脚本和锁文件、预构建 helper、本地 wheel，以及轻量 workbench 宿主机入口。
不会复制整个 test 树，也不会扫描仓库中的 Markdown 来决定打包内容。
`test/e2e/assemble_docs.py` 对完整的已选文档重写链接，不抽取章节、不改写可执行示例、
配置值或产品二进制。

#### 目录布局

| 源码位置 | 归档位置 |
| --- | --- |
| 主仓 README；快速开始、部署、系统与术语指南 | `guide/README*.md`、`guide/<topic>*.md` |
| 组件 README 与明确选择的用户契约 | `guide/<component>/<name>*.md` |
| 独立选择的 kernel 指南 | `guide/vmlinux/vmlinux*.md` |
| 主仓测试 README、QUICKSTART 与 Demo 指南 | 原有 `test/` 路径 |
| accelerator 与 guest-runtime 的 E2E README 双语文件 | `test/e2e/<component>/README*.md` |
| workbench 说明、启动器及其策略和归属声明 | `workbench/` |
| 主仓/组件的许可证、NOTICE 与 LICENSES 材料 | `guide/licenses/<owner>/<original-path>` |

组件指南列表覆盖 accelerator 的 cache/store/manifest/file artifacts、connector 的 TAP-FD
与交换机运维、guest flatten/runtime、sandbox 控制与 guest-init 契约，以及 orchestrator 的
node/build/proxy/resource/journal/telemetry 契约。存在两种语言时同时打包，已有完整英文
单语文档仍有效。四个维护中的 E2E 指南文件保留在规范扁平用例集合旁。

内部设计/补丁分析、CI/发布文档、扩展开发、源码回归测试和实验性性能工具保留在源码仓库。
指向这些材料的链接使用所选源码引用；不会因链接目标未打包就把它加入归档。源码文件不会
被删除。已选输入必须存在且为普通文件，符号链接及目标冲突会使组装失败。许可证和归属
声明保留原文，维护中的 Markdown 许可证范围文档仅重写导航。helper 和 wheel 保留原有
清单、身份与验证。平台归档不重新打包组件二进制。

#### 导航与源码版本

指向已包含文档、图片和许可证文件的链接改为归档中的实际相对路径。组件 README 移入新目录后，
双向语言选择链接随之更新。指向未装入归档的源码文件时，使用相应源码 revision 的 GitHub
链接。围栏代码块和行内代码示例保持不变。处理普通行内链接、引用定义以及 HTML
`href`/`src` 属性；复杂 Markdown 仍需要人工复核。

不带 fragment 的目录链接在包含对应 README 时沿用来源页面的语言，包括回退到组件
README 的组件 `docs/` 链接；没有中文版时回退到英文。直接文件链接和带显式 fragment
的目录链接保留指定文件或默认 README 目标，以维持原有锚点契约。

发布打包器从所选清单的 `test_revisions` 读取组件精确引用，并使用聚合版本构造主仓源码链接。
文档与用例因此引用相同源码快照；仅涉及测试调用说明的修正无需重新发布产品。
这些文档引用不改变产品版本或归档字节。直接从源码组装时，有 Git 元数据则使用 HEAD，
否则源码链接使用 `main`。本地验收可以提供制表符分隔的 `DOCS_SOURCE_REFS` 文件，每行
包含 owner 和精确源码 revision。这是组装元数据，不是运行时配置项。

runtime 与 vmlinux 单元可以选择 guest-runtime 的不同提交。因此发布打包器单独提供
`DOCS_VMLINUX_SOURCE`，从所选 kernel 源码同时取得 `vmlinux.md` 和 `vmlinux_zh.md`。
若所选旧 kernel 没有中文对应文档，不会用独立固定的 guest-runtime 测试源码中的中文文档替代。

可以识别且指向已包含文档的跨仓 `main` 链接在组装集合内解析。绝对 GitHub `main` 链接
若指向所选源码中存在的其他文件或目录，则与相对源码链接一样固定到该源码的所选引用，
并保留 query 与 fragment。若绝对 `main` URL 指向所选源码中不存在的文件，则保留原样，
需要单独检视。显式指向历史版本的 URL 保留为历史引用。外部链接,包括组件源码 URL,仍需按照
[检视策略](../CONTRIBUTING_zh.md#文档贡献)单独检查链接。

#### 验证

```sh
python3 -m unittest release/test_documentation_package.py
make test-release-tools
```

专项测试覆盖语言选择、原生构建链接、源码 URL、可执行内容不变、跨仓链接、路径冲突、
符号链接和 kernel 语言版本独立选择。发布测试还会解包实际平台 tarball，检查组件调用说明
和源码链接使用精确 test pin，而两份 kernel 文档均来自所选 vmlinux 源码，即使这些快照不同。
最终验收还必须用实际检视的源码集合执行组装、检查解包产物，
并验证全部相对路径和标题锚点。翻译完整性需要单独进行语义检视；打包检查通过不能证明
尚未完成的文档已翻译。

## 9. Preview GC

Stable 聚合发布后,该版本线进入 7 天回退窗口。`Preview GC` 从 Stable Tag 可达的
`daily-preview.yaml` first-parent 历史生成精确 allowlist,不使用宽泛版本 glob。它还会
保护:

- 任何其他仍保留的聚合 Preview 所引用的组件 Preview;
- `main` 和所有平台维护分支当前 Daily 清单引用的组件 Preview;
- 正在运行发布或删除工作流的对象。

计划阶段验证 Stable Release、Tag、声明资产 digest、canonical 清单、Preview 归属、
Release ID、可恢复源码 SHA 和 active run,并输出稳定排序计划及 SHA-256 digest。正式版
已经关闭的当前 Daily 清单不再保护同版本 Preview;其他仍开放分支和保留聚合 Preview 的
引用继续受保护。完整或残缺的 canonical Preview 都由本仓删除 wrapper 收敛。任何分页、
字段、引用或归属异常都会在零删除状态停止。手工 dry-run 不受 7 天限制,但 apply 不能
绕过窗口。

Apply 在派发任何组件删除前重新读取所有平台分支的当前 Daily 清单;计划后新增的引用会使
本轮零删除停止。所有平台分支协调器和 GC 还由同一个受支持的 concurrency key 串行执行。
GC 派发删除后,Daily 控制器在提交新清单前检查所有匹配的组件/聚合发布 run 和删除 run,
任意一个仍活跃都保持清单不变。全局串行点和两侧门禁共同避免清单选择与异步删除交叉执行。

历史聚合 Preview 在“Tag 直接指向清单提交”契约建立和完全执行前发布。GC 只对这些历史
对象使用 Stable Tag 可达的 first-parent 清单历史证明归属和精确组件选择:Release
`target_commitish` 必须与 Tag 一致;Tag 提交已有 Daily 清单时必须精确选择该 Preview,且
canonical 清单提交必须位于它的 first-parent 历史;更早的无选择清单提交则必须是
canonical 提交的 first-parent 祖先。旁支、指向其他清单和无法证明关联的移动 Tag 全部
拒绝。当前发布、残缺恢复和所有新 Preview 仍执行 Tag Commit 与清单 Commit 相同的严格
契约。

Apply 先 dispatch 五个组件仓的本地删除 wrapper。每个 wrapper 仅使用本仓短期
`GITHUB_TOKEN contents:write`,再次核对精确 Preview Tag 和源码 SHA,然后将 Release、
所有资产与 Tag 一并删除。所有组件候选消失后,平台才删除聚合 Preview。中途失败不会
重建已经删除的对象;组件和聚合删除的确定性失败都最多重跑三次,下一次根据 canonical
历史重算并继续收敛。若一次成功清理后同名对象仍可见或被重新创建,下一次 Apply 会派发
新的清理 run。Stable Release、Stable Tag、Actions artifacts、非 canonical orphan 都
不属于 GC 删除集合。

一次 Apply 会等待异步删除,每隔 30 秒根据实时 Release、Tag 和受保护引用重新生成计划
(计划执行时间另计),持续处理所有符合条件的 Stable 版本,仅在各版本候选集均为空后报告
收敛。控制器整次运行共用一小时收敛预算;到期仍有候选时报告失败,后续运行按仓库实际
状态继续。Workflow 留出 75 分钟用于准备和收尾。Dry-run 对指定或发现的每个 Stable
版本只输出一次计划,不派发删除、不等待。

```bash
# 只读真实计划
gh workflow run preview-gc.yml --repo kuasar-sandbox/kuasar-sandbox --ref main \
  -f stable_version=release-v0.5.7 -f dry_run=true

# 到达 7 天窗口后收敛
gh workflow run preview-gc.yml --repo kuasar-sandbox/kuasar-sandbox --ref main \
  -f stable_version=release-v0.5.7 -f dry_run=false
```

Workbench Preview 清理遵守同样的 canonical 版本/源码归属检查。删除自有 package version 前验证仓库关联、不可变 digest 和源码/版本 label，并保护活跃发布、其他保留 Release 的 binding、共享 Tag、保留 index 引用的子 manifest。不会 prune 无关或无 Tag 的镜像版本。历史发布保持原清理契约。

## 10. 权限与可靠性

- 跨仓 GitHub App 安装只允许 `Contents: read`、`Pull requests: read` 和 `Actions: write`,
  每个短期 token 只请求当前步骤需要的子集;
- Daily 清单提交使用同一个 App 仅面向 `kuasar-sandbox` 仓的独立短期
  `Contents: write` token;它不具有其他组件仓写入权,也不复用通用
  `github-actions` 身份;
- 组件发布、组件删除、聚合发布和平台删除只使用各仓本次 workflow 的短期
  `GITHUB_TOKEN contents:write`;
- 候选 Runner 的源码获取 token 为只读,并在执行前撤销。这不隔离持久 App/Runner
  凭据或共享可写状态;具体边界见 [CI Runner slot](../ci/runner/README_zh.md);
- 发布先创建 draft、上传并复核 digest,全部一致后才公开;
- 已发布 Tag 或 Release 不由发布器覆盖;残缺 Preview 只能经受保护删除入口恢复;
- 分支 HEAD、Tag commit、清单 blob SHA 和发行资产验证共同固定一次发布;
- 即使渲染后的 Daily 清单无需写入,协调器仍复核远端分支 HEAD 与清单 blob,不会用陈旧
  checkout 派发组件;
- Preview Release 的构建绑定防止相同 Tag/源码在不同依赖闭包之间被错误复用;
- scanner 使用独立 concurrency key;所有平台分支协调器与 GC 共用全局
  `preview-manifest-selection-and-gc` concurrency group。scanner 顺序等待每个分支,不同时
  填入多个 pending slot。组件完整构建/发布 workflow 与 delete 对同一精确版本共用
  mutation group;平台聚合完整 prepare/validation/publish workflow 与 delete 也使用同一精确
  版本组。GitHub 可能合并该组内的 pending 请求;Daily 协调器和 GC 不把 cancelled 当作
  成功。组件发布和删除按同一精确输入重跑相应 job 或完整 workflow;聚合发布创建新的
  workflow run 和 exact stage。各路径最多尝试三次;Daily 的发布或清理耗尽重试后报告失败。因此互斥不会把发布或
  删除的目标状态静默丢失。不同版本不共享 pending slot。组件 Latest 协调器是
  例外:其操作幂等且每次都
  扫描完整主线 Stable 集合,因此使用全仓串行组并允许多个触发合并;保留下来的最后一次
  运行仍能收敛完整状态。只有
  当前平台 `main` HEAD 的 `release.yaml` 所选 Stable 聚合能更新 Latest,且发布前后都会
  重新验证分支 HEAD、清单选择和既有 Release。

## 11. 受保护分支

必需的 `ci / finalize` 检查以当前双亲 integration commit 上的
`kuasar/ci-exact-head` 为依据。E2E 失败、取消或跳过都不能产生验收成功,
包括控制 job 成功但跳过 E2E 的 Draft 工作流。不能用结果桥接或独立提供的成功状态替代。

项目仓的分支保护以一个 repository ruleset 为准。当前已启用的 ruleset 只匹配
`refs/heads/main` 与 `refs/heads/release/v*`。它要求 PR、所有讨论已解决、严格匹配
`ci / finalize`、线性历史,并阻止 force push 与分支删除。不使用管理员默认绕过、签名
提交、CODEOWNERS 或 merge queue。

当前 `required_approving_review_count` 为 0。GitHub 只计入拥有 write 权限且不是 PR 作者的
approval;仓库没有独立的 write reviewer 时,强制一个 approval 会让维护者自己的所有 PR
无法按 ruleset 合入。代码检视仍通过完整 diff 自检、resolved review threads 和精确端到端集成测试
门禁执行。

启用前必须确认 `kuasar-sandbox-bms-ci` 的安装已获批 `Contents: write`。未满足此条件时
不得激活 ruleset: Daily Preview 无法提交收敛后的清单,会被唯一的 bypass 设计反向阻断。

状态检查规则对新建 branch 使用 `do_not_enforce_on_create: true`,因此可以从已经发布的
Stable Tag 建立一条新的维护线;创建后的任何更新立即回到同一套 PR 与端到端集成测试门禁，不能借此
绕过后续提交检查。

Daily Preview 必须直接把收敛后的清单提交到受保护目标分支,因此当前已启用的 ruleset 只为
`kuasar-sandbox-bms-ci` GitHub App (ID `4283831`) 配置 `always` bypass。该 App 的唯一写入
用途是上述本仓短期 token;人工维护、普通 `github-actions` 与所有其他 App 都不在 bypass
列表。CI 对 PR 始终重新验证精确 integration commit 与目标 branch,所以 bypass 不替代
`ci / finalize` 门禁。App 名称是已有注册标识,不是一种验证方法。

## 12. See Also

- [ci_zh.md](ci_zh.md):CI revision、缓存和执行模式;
- [deployment_zh.md](deployment_zh.md):部署与运行前置条件;
- [../test/QUICKSTART_zh.md](../test/QUICKSTART_zh.md):完整聚合 Release 验证;
- [../release/](../release/):选择、打包、协调、恢复和 GC 实现。

### 组件运行依赖自动发现

每个 E2E 组件在本仓 `test/e2e/lib/` 维护运行依赖（平台自有用例使用
`test/e2e/platform/lib/`）。组装器从选定的测试提交递归发现全部普通文件，
保留相对路径放入 `test/e2e/lib/<owner>/`。新增库、数据夹具或嵌套依赖时，
无需修改其他仓库的文件名清单。规范用例仍从 `test/e2e/cases/*.sh` 自动发现。

以 `test_` 开头的名称、Python 缓存目录（`__pycache__`、`.pytest_cache`）和
编译缓存文件（`*.pyc`、`*.pyo`）按约定仅用于源码自测，不得作为运行依赖。
组装器拒绝符号链接、非普通文件、缺失或空的运行库以及重复用例 ID；
执行时不会通过额外下载补齐缺失输入。归档校验与执行验证仍针对实际交付包。
