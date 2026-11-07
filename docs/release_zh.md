[English](release.md) | [简体中文](release_zh.md)

# Release

本文是维护者选择组件版本、生产和发布聚合资产、恢复发布失败的契约。
使用者取得软件参照[安装](installation_zh.md)，部署运维者更换版本参照[升级](upgrading_zh.md)。
CI 准入和执行证据由 [CI](ci_zh.md) 维护。

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

每个受维护的平台分支使用两份版本选择清单：

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

新聚合选择必须显式声明 `delivery: workbench-v1`。省略 `delivery` 表示历史读取契约，不能用于新聚合组装。

### 2.1 Stable 清单

首个 Stable 可省略 `previous_version`;提供时必须指向更早的 Stable。
首个 Stable 的发布说明不以 Preview 或整个仓库提交历史代替比较基线。

```yaml
version: release-v0.5.7
delivery: workbench-v1
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
delivery: workbench-v1
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

新 Stable 与 Daily 清单只选择六个组件 **Tag**，拒绝独立的测试、helper 或文档源码覆盖。每个 owner 的产品源码、用例、helper 和指南来自所选单元 Tag；guest-runtime 使用 runtime Tag，kernel 使用独立的 vmlinux Tag。消费旧版本时，从该聚合 Tag 下的 manifest 确定六个单元，不能用当前 main 或组件 Latest 重新解释。历史机器记录被忽略，不查询旧 Actions 来决定已发布包能否使用。

聚合按具名 Tag 获取源码，并在获取前后核对自动解析的提交身份。源码 checkout 与 `source-records.json` 作为本次运行输入传递；本次构建检查所选 Tag 和干净源码输入。helper 包仅携带运行字段及文件完整性信息。组件指南的源码链接使用所选 Tag。复用旧产品 Tag 时，不会从更新分支补取测试或指南。

组件新构建仍固定本次源码、所选依赖、构建顺序与输入，执行最终组合验收。已发布包按精确 tag 复用，不从 Release 正文或旧 Actions 证明历史测试资格。发布当次的临时 stage、plan、哈希和测试结果独立于人类说明，且不成为 Release 协议。

普通测试或文档变化不扩大产品输入差异，也不自动推动产品或 kernel 版本。需要交付这些变化时，显式选择正常发布的新 owner Tag。声明缺失时明确失败，不借用更新 HEAD，也不伪造声明。

历史清单从其聚合 Tag 读取。旧测试或来源记录被忽略，不用于独立选源，也不查询旧 Actions。消费者只需要该版本完整的发布包及有效的 GitHub 原生大小和摘要；仅在本次操作确实需要源码时才按对应单元 Tag 获取。

helper 构建按已提交的版本与哈希锁获取 Python 3.12 Demo wheel 闭包；打包要求两种架构的本地 wheel 及其包名、版本和摘要清单，公开 prepare 仅从这些 wheel 离线安装。

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

平台仓的 GitHub Latest 只由平台 `main` 的 Stable 聚合发布更新。每个组件仓通过按仓串行的发布器独立维护自己的 Latest，版本与发布时间的比较规则见[组件发布](#6-组件发布-cli)。组件维护分支 Stable 和任何 Preview 不更新 Latest。平台聚合始终通过清单中的精确 Tag 选择组件，不依赖组件仓的 Latest。

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
已发布组件按所选 Tag 和原生资产元数据作为成品复用，不读取或审核历史依赖 marker。新构建仍固定本次依赖输入，并进行最终组合验收。
vmlinux 不因这些依赖变化重建。同日同修订号已发布后再次需要重建时保持 deferred；可显式选择更大的修订号，
例如 `gh workflow run daily-preview.yml --ref main -f date=20260914.1`。
重复请求恢复相同发布，不覆盖完整 Release，不自动递增修订号。
新发布组件与聚合使用相同日期/修订后缀，复用版本保留原始字节。

平台自身在所选分支上的代码变化同样会产生新的聚合 Preview,即使六个组件都复用。
同一聚合版本的 workflow run 名同时绑定平台源码 SHA。只要任意匹配 run 仍在运行,即使
更新的 run 已取消或结束,控制器也保持当前 Daily 清单不变;分支产生新 HEAD 后使用新的
run 身份,不会用旧输入重跑或改写运行中清单。
分支协调任务的身份还包含请求日期,因此相同平台 SHA 的不同日期扫描不会错误复用彼此的
结果。

若依赖重建已写入 Daily 清单,但对应组件 Release 尚未发布,后续扫描会继续完成这个
待发布 Preview,即使所选依赖已与清单一致。获胜 Tag 为 Stable 或 Preview 时均适用;
日期或修订号推进后,使用新选择的后缀发布重建结果。完整 Release 按所选 Tag、包集合和原生资产元数据复用，不审核历史依赖绑定。

### 4.3 首次迁移到 Tag 选择的输入

先提供平台源码契约机制，再提供依赖该机制的组件生产工作流。确认每个 owner 源码分支包含本仓指南声明和全部必需输入，然后**单独评审**选择正常新 owner Tag 与聚合版本的清单变更。

引入 helper 声明时，先在既有 CI 框架下合入组件声明及配方，再启用平台的声明发现。平台变更随后可按 §8.1 的只读证据边界使用原已发布基线进行验证。新合入的声明不能替代旧 Tag 中缺失的声明；单独评审的首次 Tag 选择使这些声明进入新发布。

首代能力需求声明必须让六个测试 owner 一起迁移。先在既有框架下合入单独的新 Tag
Preview 选择，并保留完整的已发布前序版本。平台框架 PR 使用五个仍开放的 owner 声明
PR 作为 companion 验证；每个 companion 都必须具有针对当前提交的独立集成检查成功
结果。先合入框架，再合入这些 owner PR，从已合入的分支发布所选组件 Tag。完成按能力
调度的聚合 exact-assets 验证后，才算完成切换。在该聚合发布前，混合新声明与旧选定
Tag 的普通源码请求会明确失败。这个临时失败和源码 CI 成功都不授权绕过发布检查、
为已发布 Tag 借用分支文件，或发布混合代际的聚合。

Preview 对已提交的选择使用 §6 的正常组件发行入口，按依赖顺序先 accelerator 和 connector，再 sandboxer，最后 orchestrator 和 runtime。机制合入后，先完成新 Preview 的完整验收，再进行 Stable 切换；Stable 随后使用 formal coordinator 的已提交选择。所选组件 Release 就绪后，使用 §7 的正常聚合入口并执行 §8 的全部验证 gate。若相关 kernel 输入未变化且所选文档完整，独立选择的 vmlinux Tag 可以保持不变。

首次显式迁移与通用 Daily 的仅文档变化复用算法分开；后者不能替代经评审的新 Tag 选择。不得扩大产品变化输入或添加强制构建规则。历史 Tag 与发布包按 §2 的只读契约保持不可变。

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

完整 Release 必须具有所选 Tag 对应的精确包集合、匹配的 prerelease 状态，以及有效的 GitHub 原生资产 ID、大小、digest 和上传状态。忽略可选的历史 SHA256SUMS。历史单架构发布仍有效，架构集合残缺才算残缺；新 Workbench 聚合需要全部十五项包。缺失或有歧义的资产元数据须保留并报告，不能据此把发布判作残缺而删除。

只有完整 Release 的 Tag 才作为成品复用候选。残缺发布保留给恢复流程：

- 有活动发布或删除时延后协调；完整已发布成品不触发历史 Actions 查询。
- 修改渠道选版前，先按原源码恢复当前维护聚合的残缺发布；同日 main 推进后的重试也遵守此规则。存在已配置的残缺组件 Preview 时，先保持当前清单的整套依赖版本恢复，再选择新版本。
- 仅当实际前置 job 成功且全部必要临时 artifact 仍可用时，恢复失败的 publish job。组件需要双架构归档及原 Workbench selection；聚合需要 stage、validation 和必要 Workbench 结果。尚未创建 Release 或 tag 时也可恢复。
- 新任务在准备阶段取消，不会遮蔽更早的成功 stage。多个可能拥有发布字节的 stage、源码冲突、artifact 缺失或过期均保留并报告；无法恢复时明确选择新的 Preview 标识，不先删除残缺字节再重建。
- publish 已成功而后续 cleanup 失败时，等待真实 Release API 收敛。没有成功 stage 或任何已发布对象时，失败构建才可新建 run。构建派发、发布恢复及清理各在当前协调调用中最多尝试三次，历史 attempt 不会锁死后续调用。精确匹配的恢复 run 超过二十个时停止并要求明确处理，避免无限扩大扫描。

新日期或修订号不会把旧 tag 绑定到新的组件 HEAD。先完成原残缺发布再推进渠道，或保留旧对象并明确使用新标识。已完成但不再被引用的组件 Preview 仍遵守现有 canonical GC 范围和七天回退窗口。

版本和字节一致且成功 stage 仍在时，优先恢复原发布步骤；只补缺失资产，拒绝同名不同字节。已公开 Release 只能对原验收 stage 只读确认。清理残留无 tag Release 时，必须具有已知精确源码，并通过与计划相同的 canonical 历史检查；GC 不要求旧源码仍为当前分支 HEAD。Release 和 tag 均不存在则已收敛。明确 404 可幂等重试，网络或权限错误不视为不存在。

## 6. 组件发布 CLI

Daily 自动观察源码 SHA。手动组件发布可选择精确的历史源码 SHA；版本与依赖选择仍使用 Tag，不使用 SHA 作为版本。CLI 示例中的 `OWNER_REPOSITORY`、`SOURCE_REF` 和 `PLATFORM_REF` 分别表示 owner 仓库、源码分支和聚合分支。

组件 workflow 始终从仓库 `main` 加载受信任工具，但必须显式传入允许的源码分支及该分支 first-parent 历史中的精确提交 SHA。以下示例定义匹配的仓库与分支，用于自动身份查询和派发；自动 Daily 提供相同输入：

```bash
OWNER_REPOSITORY=kuasar-sandbox/accelerator
SOURCE_REF=release/v0.2.x
PLATFORM_REF=release/v0.5.x
gh workflow run release.yml \
  --repo "$OWNER_REPOSITORY" --ref main \
  -f version=v0.2.2-preview.20260831 \
  -f source_ref="$SOURCE_REF" -f source_sha="$(gh api "repos/$OWNER_REPOSITORY/git/ref/heads/$SOURCE_REF" --jq .object.sha)" \
  -f aggregate_version=release-v0.5.7-preview.20260831 \
  -f aggregate_sha="$(gh api "repos/kuasar-sandbox/kuasar-sandbox/git/ref/heads/$PLATFORM_REF" --jq .object.sha)"

OWNER_REPOSITORY=kuasar-sandbox/sandboxer
SOURCE_REF=release/v0.3.x
PLATFORM_REF=release/v0.5.x
gh workflow run release.yml \
  --repo "$OWNER_REPOSITORY" --ref main \
  -f version=v0.3.6-preview.20260831 \
  -f source_ref="$SOURCE_REF" -f source_sha="$(gh api "repos/$OWNER_REPOSITORY/git/ref/heads/$SOURCE_REF" --jq .object.sha)" \
  -f aggregate_version=release-v0.5.7-preview.20260831 \
  -f aggregate_sha="$(gh api "repos/kuasar-sandbox/kuasar-sandbox/git/ref/heads/$PLATFORM_REF" --jq .object.sha)" \
  -f accelerator_version=v0.2.2-preview.20260831 \
  -f connector_version=v0.1.9
```

orchestrator 接收 `accelerator_version`、`connector_version` 和 `sandboxer_version`;
runtime 只接收与镜像实际载荷一致的 `accelerator_version` 和 `sandboxer_version`;
vmlinux 不接收内部组件版本输入。runtime 与 vmlinux 使用各自工作流。
预检获取允许的 `main` 或匹配版本线的 `release/vMAJOR.MINOR.x` 分支，要求 `source_sha` 属于其 first-parent 历史。Daily 默认仍选择分支 HEAD；手动指定历史提交时，产品、测试、helper 声明、配方与文档全部来自该提交，不从新版补齐缺失契约。预检保存准入时的分支 tip、所选 SHA/tree、分支、单元与版本，并单独记录可信框架及 workflow 修订。freeze 复用这份不可变准入输入，不再次抓取推进中的分支；双架构构建恢复同一份源码，最终 Tag 指向所选提交。从 main 准入的历史提交仍按主线处理 Latest，维护线语义不变。Preview 不是所有组件发布的强制前置条件；已发布组件按 tag 复用，由最终组合验收检查所选成品。PR merge-ref 与聚合 branch-HEAD 的严格准入不变。依赖参数必须对应已经完整发布的精确 Release；预检按所选 Tag 获取并固定校验后的源码输入。每种架构在独立的原生 job 中构建。
Preview 还要求 `aggregate_sha` 所指平台提交中的
`daily-preview.yaml/version + preview_version` 精确等于聚合版本,且
`components.<unit>` 精确等于待发布 Tag。预检固定这份清单绑定；取得 publish concurrency lock 后，发布核验固定证据并检查实时 Stable 封线状态，不重新获取更新后的分支清单。这些准入输入仅留在当次流水线。Release notes 只含人类说明，新组件包只保留运行载荷和许可证，不携带独立来源／构建清单或校验和资产。复用旧成品时忽略历史 marker 和 SHA256SUMS。

工作流名称中的源码 SHA 和依赖版本元组仅用于定位当前未完成发布。匹配的成功 stage 恢复 publish；源码或依赖冲突时保留 stage，要求新的选版。打包工具来自 `github.workflow_sha` 固定的主线工作流提交，与选定产品源码分离。组件 publish 按仓库排队；仅从 main 准入并成功发布的 Stable 可以更新 Latest，Preview 和维护线均不更新。同一单元检查版本倒退；guest-runtime 的 runtime/vmlinux 分别比较自己的版本线，跨单元比较共享 Latest 时使用发布时间。不再运行独立 Latest 协调器。


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


Stable 准备需要创建平台清单提交时，通过 `PLATFORM_TOKEN` 提供已获授权的平台 writer token（仓库 contents 写权限）；组件工作流访问仍独立。也可先运行 `python3 -B release/prepare_notes.py . release`，编辑审阅正文，经正常评审流程提交到分支后再执行 `make release`；同一已准备选择不会写平台分支，因此不需要平台 writer token。不要把 token 写进清单或命令记录。

聚合工作流同样从平台 `main` 加载受信任工具,但版本选择、系统文档、平台用例和打包
内容来自所选平台分支的精确 HEAD。目标分支中的旧发布脚本不会作为控制器执行:

```bash
PLATFORM_REPOSITORY=kuasar-sandbox/kuasar-sandbox
SOURCE_REF=release/v0.5.x
gh workflow run aggregate-release.yml \
  --repo "$PLATFORM_REPOSITORY" --ref main \
  -f version=release-v0.5.7 \
  -f source_ref="$SOURCE_REF" \
  -f source_sha="$(gh api "repos/$PLATFORM_REPOSITORY/git/ref/heads/$SOURCE_REF" --jq .object.sha)"
```

正式发布前先在目标分支提交 `release.yaml` 选定版本；`make release` 会先准备并持久化完整正文，再发布缺少的 Stable 组件并从该精确提交触发聚合。需要预先审阅时，先运行 `release/prepare_notes.py` 并编辑、提交正文。主线 Stable 成功后成为 Latest;维护分支 Stable 保留为可发现的正式版本,
但不抢占主线 Latest。Stable 与 Preview 都必须由该 HEAD 当前清单直接选择;历史清单只
用于解析已存在 Release 和 GC,不能通过手工 dispatch 回填成新的聚合发布。

聚合 prepare 生成的短期 artifact 是一次 run 的不可变发布证据。构建/验证失败后重新 dispatch 相同版本、源码分支和源码 SHA，确保新的 prepare 重新下载当前组件 Release。
新契约的残缺发布失败后，由控制器验证原先的成功前置 job 和保留 stage，只重跑该 publish job；
不要完整重跑旧 workflow 来替换镜像字节。Daily 可自动恢复原发布 job；Stable 不会为已经完成、正在发布、残缺或已按另一源码准入的相同版本重新选版，应走原 publisher 恢复路径。每次协调调用对原 publish job 最多恢复三次；过去的传输失败不会永久禁止以后恢复同一成功 stage。

本地发布工具验证入口为:

```bash
make test-release-tools
make test-ci-tools
```

## 8. 发行资产验证

组件包按单元、版本和架构精确命名；runtime 与 vmlinux 独立解析。新 `workbench-v1` 聚合包含十五项资产：一个平台包、十二个不变的组件包、两个原生 Workbench 镜像包，不再发布独立 `SHA256SUMS`。读取器忽略历史 checksum 附件，从所选聚合 Tag 下的 manifest 确定完整集合；缺包不会回退旧格式、main 或 Latest。

不发布项目生成的 release metadata JSON,也不重复上传 GitHub 自动提供的源码归档。
版本选择 YAML 只在仓库维护,既不是 Release 资产,也不进入 platform 包。
组件 archive 不携带 `docs/` 或 `test/e2e/`；aggregate assembly 从所选 owner Tag 收集组件文档
和 E2E 输入，统一写入 platform archive（§8.1）。kernel 文档使用单独选择的 vmlinux 产品源码。

组件按固定输入在 x86_64 与 aarch64 独立原生 job 中构建、测试，保留正常源码 CI。聚合按固定 GitHub asset ID 下载六个所选 Release，校验原生 size/digest、安全路径、权限和跨包冲突，构建所选 tag 的 helper、platform 和 Workbench 包。最终结构、安装、启动、组合、公开 prepare/run 与 Workbench 验收使用本次暂存字节，不重建产品或 helper。聚合 exact-assets 不再重复组件源码 unit/race/vet，汇总明确报告 skipped。发布必须核对本次双架构和 Workbench 成功结果，并按已验收 plan 比较上传后的完整名称、大小、digest 和状态。plan、哈希和结果仅用于当次流水线，不写入 Release 正文或包内。

Preview、维护分支 Stable 和主线 Stable 使用相同资产与发行资产验证门禁;差别只在发行状态与
Latest 策略。

<a id="平台包中的文档"></a>
Workbench 的 prepare 与构建期 collector 共享规范用例发现和外部镜像请求函数。collector 在同一暂存请求中只解析一次移动 Tag，并用原始 registry manifest/config 证据绑定所复制的 Docker archive。原生构建每种架构的单个 gzip Docker 镜像归档，标注相同聚合版本和精确源码。实际压缩字节必须**小于 2 GiB**；超限或残缺输入在暂存/发布前失败。镜像只包含工具和外部输入，六个产品归档保持独立且与上游字节一致。

新增原生 workbench 门禁导入这些精确暂存字节，从空的私有 Docker 状态、无外部路由开始，通过公开入口离线 prepare，再仅重新连接所属 bridge 执行 run，保留 Demo 真实的 Guest Internet 出站断言。具备 KVM 的原生 x86 和 ARM 使用相同的当前完整常规用例选择；GitHub 托管 ARM runner 没有 KVM 设备：其显式 `artifact-only` workbench 结果验证归档、导入镜像及原生发行输入，单独记录计划用例，不声称完成系统或离线执行。ARM 原有原生构建及非 KVM E2E 通道仍为必需。完整原生 ARM workbench 验收使用已发布字节及与 x86 相同的 `--all --exclude storage.obs.sh` 选择，包含 orchestrator、builder、telemetry 和静态无 libc cgroup probe。所选 Guest 内核必须提供实际 PMEM/EROFS `dax=always`，保留架构及运行断言。用例列表由精确发行文件产生，不固定为历史数量。旧的有限 workbench 验收仅对其原有用例范围有效。私有 daemon 隔离、实际所选 KVM/UFFD/TUN/BPF 操作及所有权清理都必须通过。Workbench 系统模式使用 Docker privileged 权限，面向可信系统管理员；自定义宿主 AppArmor/seccomp 策略工具及权限收缩专项证明不再是部署或发布前置条件。通用系统启动报告不可用的硬件能力，不冒充相关产品测试已经通过。工具链环境不能替代单独的无编译器 runtime 门禁。新的原生结果声明 `native-full`，历史 `system` 保留原有选择范围，托管 ARM 继续显式声明 `artifact-only`。这些记录绑定精确 framework、测试来源事实、产品摘要、archive/config 身份、实际压缩大小、压缩/导入时间和观测到的磁盘峰值；系统结果另含启动时间及已执行用例证据；磁盘观测不是配额。

发布把已测试的保存镜像复制到 `ghcr.io/kuasar-sandbox/workbench:<去掉-release-前缀的聚合版本>`，验证两种架构的 config 身份和多架构 index，并匿名回读每个按 digest 固定的镜像到临时 Docker archive。归档校验器将每个解压后的镜像层与已测试离线镜像的 config 逐一绑定，发布重试也必须通过。registry digest、镜像 ID、离线 archive hash 含义各自独立。已有同名 Tag/资产必须匹配；重试只补传缺失资产，不覆盖测试字节。跨服务发布可恢复：只有 registry 和完整 GitHub 资产集合一致，GitHub draft 才公开。冲突时失败关闭，不改写历史源码和已发布字节。

`release/` 存放发布脚本，`release/inputs/` 存放共享构建输入，测试放在 `test/release/`。`releases/` 只包含两个渠道清单：Stable 使用 `releases/release.yaml`，Daily 使用 `releases/daily-preview.yaml`。两个选版协调器会生成完整说明，并与选定版本一起持久化。生成时读取前次选定 tag 到各组件计划 source SHA 的完整提交范围，不必等待未来组件 tag；逐条列出提交标题、commit 链接，以及 GitHub merge/squash 标题中的所属仓库 PR 链接，直接提交也保留。比较链接固定源码，区分源码相同、回退和分叉。内核清单仅列触及内核发布输入的提交，runtime 则明确标注为共享仓库历史。API 清单不完整时停止，不提交残缺说明。自动正文优先展示渠道、比较基线、组件版本和本次合入，再提供简明的下载方法与安全提示；常驻介绍改用文档链接，避免重复宣传。清单仍是可编辑发布说明的唯一来源，发布器不重新生成合入清单，人工说明不属于产品验收内容。

```yaml
notes:
  mode: auto
  body: |
    # 完整发布说明

    在此填写变化、升级步骤和兼容性限制。
```

`mode: auto` 在选版变化时重新生成；同一选择重试会原样保留人工编辑及格式。聚合版本、组件版本、前序版本或交付契约改变都属于新选择。协调器直接比较更新前后的非 notes 选版字段，不保存生成指纹。`mode: fixed` 后续任何选版变化都保留完整正文；对可识别的旧标题、组件表或文档链接仅警告，不覆盖或拒绝固定说明。

需要编辑说明时，在同一次未提交修改中先更新渠道版本，再运行 `python3 -B release/prepare_notes.py . release`（Daily 使用 `daily-preview`）。组件 tag 尚未发布时，用 `--source-sha UNIT=SHA` 传入已经选定的完整 SHA，可为多个未发布组件重复传入；协调器会自动提供这些身份。命令将工作区选择与 HEAD 中已提交的清单直接比较；已有正文且选择未变时不重新生成，首次缺少正文时初始化。如果选择和 auto 正文均已修改，命令停止而不覆盖待提交文本；请审阅后一起提交，再重试。生成过程仅需 GitHub 仓库只读权限。编辑 `notes.body`，需要固定时设置 `notes.mode: fixed`，然后提交。`--check` 只检查文本是否存在和语法，不写文件，也不证明源码或测试来源。Stable 的 `make release` 与 Daily 使用相同准备规则。单文件提交以读取到的精确分支 HEAD 为原子前提，人工编辑或其他文件并发变动都会阻止派发。提交响应丢失也停止派发；检查分支后从新 checkout 重试，已提交且未变化的选择不会重复生成或提交。

正文缩进四个空格。支持 literal `|`、`|-` 及其结尾换行语义，保留 Markdown、空行、行末空格和 Unicode。`body` 是整段人工发布说明，不再插入 Highlights。没有 `mode` 的旧清单仍可读取；准备步骤会把旧 `body` 和 `upgrade_notice` 一起迁移进完整正文，不能丢弃升级提醒。显式模式不能同时使用 `upgrade_notice`。缺少模式不是拒绝发布说明的理由。

发布器读取目标聚合版本最新提交的渠道说明。渠道已进入新版本时，沿该清单的 first-parent 历史查找目标版本，不误用新版本文字；发布器历史中没有的维护分支版本可以使用自身清单。新说明不需要源码 SHA、plan ID、修正标记、与产品一致的组件表或验收结果。历史 `notes.aggregate_sha` 和 `notes.selection_sha256` 字段读取时忽略，准备清单时清除。人工说明不与已测试源码或 stage 中的旧文字比较。原 plan、产品源码、制品、架构测试和 Workbench 结果仍独立校验。已有验收 stage 通过原发布任务恢复，不重新选版或重建产品。包消费者忽略历史 Release 正文。

选版仅更新清单，不在此时限制正文长度。发布前的 125000 字符上限只适用于完整人类说明。超限则停止并报告实际长度与预算，等待人工修改并提交清单；发布器不压缩、截断或写回说明。正文不携带验收、依赖、来源或 registry 机器记录。成功 stage 仍在且版本和字节一致时，优先恢复原发布步骤；stage 过期或冲突须明确失败并重新验收，不建立永久恢复证据库。

用户只解压所选产品架构与 `platform-release`；`workbench-<arch>-v*.tar.gz` 使用 `docker load` 导入，不能解压到产品目录。[安装](installation_zh.md) 检查声明集合并按显式资产类别下载。用户可以不下载 workbench，但新聚合发布必须包含两种架构镜像。

### 8.1 文档载荷与源码映射

平台归档使用 `test/e2e/package_inputs.py` 中的仓库本地输入发现。组件输入来自
所选 owner/单元 Tag，kernel 指南来自独立选择的 vmlinux Tag。组装器按目录约定
发现规范用例和运行库，通过所选仓库的指南输入清单发现指南，同时复制
Demo 脚本和锁文件、预构建 helper、本地 wheel 与轻量 workbench 宿主机入口。
不会复制整个 test 树，也不会打包未声明的指南。
`test/e2e/assemble_docs.py` 对完整的已选文档重写链接，不抽取章节、不改写可执行示例、
配置值或产品二进制。

平台 helper 使用 `release/inputs/helper-inputs.yaml` 和固定的 `release/inputs/helper-tools.yaml`。组件和旧 tag 可以继续使用 `release/helper-inputs.json`。平台的 helper 及指南清单均使用兼容 JSON 的 YAML 格式。Schema `1` 声明完整的 `helpers` 清单（`name`、可选 `env` 和 `cases` 中的用例文件名模式）、仓内相对路径 `recipe`，以及 `dependencies` 中的依赖 owner。没有 helper 的 owner 显式声明空清单和 null 配方。通用构建器在 owner 根目录中使用固定依赖工作区调用 `bash <recipe> <architecture> <output-directory>`；内部构建命令由该仓库维护。每个声明输出必须是权限安全且符合目标 ELF 架构的普通可执行文件。未声明、缺失或重复的输出均失败。

本次构建计划固定所选源码、声明和配方输入，相关临时检查留在当次流水线。新包只包含每架构 schema `2` 的 `helpers.json`：helper 名称、owner、环境变量、适用用例、文件大小、SHA-256 与权限。两架构的运行清单必须一致，并交付所有声明 helper；旧 alias 表不会限制其数量。包内不保留 source/tree、测试修订、recipe/blob、原始 plan 或 producer 收据。

公共 prepare/run 校验实际 helper 文件、ELF 架构、安全路径与权限以及运行字段。旧包仅提取运行字段，不查询历史 Actions，也不把来源证明写入 prepared workspace。源码构建必须具有所选 owner 的声明和完整本地输入；缺失时明确失败，不借用新版源码或历史验收。

#### 组件运行依赖自动发现

每个 E2E 组件在本仓 `test/e2e/lib/` 维护运行依赖（平台自有用例使用
`test/e2e/platform/lib/`）。组装器从选定 owner Tag 递归发现全部普通文件，
保留相对路径放入 `test/e2e/lib/<owner>/`。新增库、数据夹具或嵌套依赖时，
无需修改其他仓库的文件名清单。规范用例仍从 `test/e2e/cases/*.sh` 自动发现。

以 `test_` 开头的名称、Python 缓存目录（`__pycache__`、`.pytest_cache`）和
编译缓存文件（`*.pyc`、`*.pyo`）按约定仅用于源码自测，不得作为运行依赖。
组装器拒绝符号链接、非普通文件、缺失或空的运行库以及重复用例 ID；
执行时不会通过额外下载补齐缺失输入。归档校验与执行验证仍针对实际交付包。

#### 仓库自行声明交付文档

平台从已接纳源码的 `release/inputs/guide-inputs.yaml` 读取字符串数组。组件和旧 tag 的 `release/guide-inputs.txt` 继续按行读取。
条目可以是仓内相对 Markdown 路径、目录（递归发现 Markdown）或 glob 模式；
旧文本清单忽略空行和 `#` 注释。每个条目必须匹配；声明缺失或为空、越界路径、符号链接、
显式选择非 Markdown 文件以及重复匹配均使组装失败。目录发现保留 `docs/` 下的嵌套路径。

平台仅维护本仓声明，各组件自行维护用户文档与开发文档的边界，聚合器不再保存
逐组件文件名清单。许可证材料沿用 LICENSE/NOTICE/COPYING 命名约定。
独立版本单元按 `docs/<unit>.md` 及可选 `_zh.md` 对应文件约定，从该单元的
精确选定 Tag 读取，包括不可变旧 Tag。

<a id="resource-pool-availability"></a>
#### 资源池的版本可用性

资源池配置与按容量恢复由
[Orchestrator #515](https://github.com/kuasar-sandbox/orchestrator/pull/515)、提交
`766732d787db79ecb18882e4157bc01b8404168d` 引入。本次核验的 Stable 选择
`v0.1.12` 与 Daily 选择 `v0.1.13-preview.20261106` 均不包含该变更及资源池契约。
不要向这些版本照搬新的 `resource_pool` 配置。启用该能力前，应选择包含它的组件发布版，
并按所选组件版本的完整指南使用。文档移动不会为旧产品增加运行时能力。

导航优先使用所选 `node-resource.md#resource-pools` 契约；旧源码中存在
`resource-pools.md` 时使用该完整契约。两者都不存在时，链接到本版本说明，
不会把旧容量预算章节冒充为新功能契约。

#### 带版本的导航迁移

Owner 可以在所选源码中声明少量、明确的 `release/guide-migrations.json`，使独立选择版本的文档在重组后仍可导航。它是装配元数据，不是新的读者指南，也不要求永久保留旧 Markdown 页面。没有该文件的历史 Tag 仍然有效。

```json
{
  "schema": 1,
  "redirects": [
    {"from": "docs/kuasar-sandbox.md", "to": "docs/architecture.md"}
  ]
}
```

`from`/`to` 是仓库相对 Markdown 路径，可包含 fragment。可选 `to_owner` 默认本 owner；跨 owner 目标必须属于本次选中的源码输入。装配器只读取这些精确输入中的声明，拒绝不安全路径、symlink、未知字段、重复来源、循环和无效声明目标。

解析针对实际选入输出的文档，包括独立选择的 VMLinux 源码：原目标与 fragment 有效时优先保留；否则使用显式迁移调整路径/fragment，或在去掉章节编号前缀后找到唯一匹配标题，并输出所选原文真实存在的 anchor。对已选目标或命中迁移的 fragment，歧义或无匹配均使装配失败。整页迁移不能吞掉未知 fragment，也不能降级到 README。显式指向历史修订的 URL 保持原样；所选原文、示例与许可内容不会被当前正文替换。

Schema `2` 保留 `redirects`，并增加用于已核实跨版本引用的 `fallbacks` 数组。
每项声明 `from_owner`、`from`、`to`，可选 `to_owner` 默认 `from_owner`。
原始引用有效时始终优先，其次使用结果 fragment 可解析的所选 owner 显式 redirect。同一来源的 redirect
与 fallback 只有目标完全相同时才能并存；目标不同则拒绝。两者都不能直接解析时，
显式 fallback 链必须落到所选源码树中真实存在的
文档与 fragment。目标未列入交付指南时，仍按该 owner 的选定修订生成固定源码 URL；
不会从较新 checkout 借正文，也不会在包内制造替代契约。循环、缺失的链终点、未知或
歧义 fragment 都使装配失败。整页 fallback 继承请求的 fragment，不能偷偷改成
fallback 页自带的 fragment。

这些声明维护具体核验过的源码组合，不承诺任意历史版本混配。例如，镜像格式准备契约
从 Guest Runtime 的 `docs/flatten.md` 移到 Accelerator 的 `docs/image-preparation.md`。
选中新 Accelerator 时，即使 Guest Runtime 较旧，也使用所选 Accelerator 的真实契约。
旧 Accelerator 配合已经移除旧契约的新 Guest Runtime，可能使两个位置都不存在；
装配此时明确报出缺失终点。应选择保有契约的版本组合，不能把该引用改到 README。

只为有证据的导航兼容需求维护迁移。Owner 移动文档时同步更新并验证当前源码、独立选择的历史内核、双语以及仅有英文的旧输入。

#### 目录布局

| 源码位置 | 归档位置 |
| --- | --- |
| 主仓 README | `guide/README*.md` |
| 主仓文档目录（`docs/README*.md`） | `guide/index*.md` |
| 选中的平台概念、架构、使用和运维指南 | `guide/<topic>*.md` |
| 组件 README 与明确选择的用户契约 | `guide/<component>/<name>*.md` |
| 独立选择的 kernel 指南 | `guide/vmlinux/vmlinux*.md` |
| 主仓测试 README、QUICKSTART 与 Demo 指南 | 原有 `test/` 路径 |
| accelerator 与 guest-runtime 的 E2E README 双语文件 | `test/e2e/<component>/README*.md` |
| workbench 说明、启动器及其策略和归属声明 | `workbench/` |
| 主仓/组件的许可证、NOTICE 与 LICENSES 材料 | `guide/licenses/<owner>/<original-path>` |

各组件选择完整能力文档，包括理解运行时契约所需的参考和设计章节。平台选择包含文档目录、
完整实例/模板任务、部署、安全、运维、升级和性能指南。根 README 与文档目录具有不同归档路径。
维护中的两种语言一起交付，完整英文单语文档仍有效。E2E 指南双语文件保留在规范扁平用例集合旁。

贡献流程、未选中的示例、CI/发布维护、源码测试和实验工具保留在源码仓库。
选取范围由所属仓库声明定义，不在这里再维护一份文件名清单。指向未打包材料的链接使用
所选源码引用；不会因链接目标未打包就把它加入归档。源码文件不会
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

发布打包器从所选清单的 `components` 读取组件 Tag，并使用聚合版本构造主仓源码链接。
文档与用例因此引用同一所选 Tag；交付更新的测试调用说明需要显式选择正常发布的新 owner Tag。
这些文档引用不改变产品版本或归档字节。直接从源码组装时，有 Git 元数据则使用 HEAD，
否则源码链接使用 `main`。本地验收可以提供制表符分隔的 `DOCS_SOURCE_REFS` 文件，每行
包含 owner 和精确源码 revision。这是组装元数据，不是运行时配置项。

runtime 与 vmlinux 单元可以选择 guest-runtime 的不同提交。因此发布打包器单独提供
`DOCS_VMLINUX_SOURCE`，从所选 kernel 源码同时取得 `vmlinux.md` 和 `vmlinux_zh.md`。
若所选旧 kernel 没有中文对应文档，不会用单独选择的 runtime 源码中的中文文档替代。

可以识别且指向已包含文档的跨仓 `main` 链接在组装集合内解析。绝对 GitHub `main` 链接
若指向所选源码中存在的其他文件或目录，则与相对源码链接一样固定到该源码的所选引用，
并保留 query 与 fragment。若绝对 `main` URL 指向所选源码中不存在的文件，则保留原样，
需要单独检视。显式指向历史版本的 URL 保留为历史引用。外部链接,包括组件源码 URL,仍需按照
[检视策略](../CONTRIBUTING_zh.md#文档贡献)单独检查链接。

#### 验证

```sh
python3 test/release/test_documentation_package.py
make test-release-tools
```

专项测试覆盖语言选择、原生构建链接、源码 URL、可执行内容不变、跨仓链接、路径冲突、
符号链接和 kernel 语言版本独立选择。发布测试还会解包实际平台 tarball，检查组件调用说明
和源码链接使用所选 owner Tag，而两份 kernel 文档均来自所选 vmlinux 源码，即使这些快照不同。
最终验收还必须用实际检视的源码集合执行组装、检查解包产物，
并验证全部相对路径和标题锚点。翻译完整性需要单独进行语义检视；打包检查通过不能证明
尚未完成的文档已翻译。

同一次发布的 Workbench 分组结果保留两次独立 x86 执行与必需的 ARM 结果。stage 校验核对本次实际字节、job/attempt 与输入生产任务；新的失败或重跑会使旧结果失效。这些仅为临时流水线记录，不进入 Release 正文，也不是已发布包的消费资格协议。

兼容 stage 读取器必须先合入 main，再启用分组生产。默认仍为完整调度，直到真实端到端测量与失败重试验收证明分组合理，详见 [CI](ci_zh.md#workbench-运行分组)。正文不预留 registry 空间，普通发布包消费不依赖历史分组。

## 9. Preview GC

Workbench GC 使用实际非目标 registry tag 与 multiarch 引用保护共享镜像，也保护保留的无 tag index。只删除明确属于目标 Preview 的 index 及其独占子镜像，先删 index；每次删除前刷新引用。明确 404 可幂等重试，网络或权限错误则停止。不确定归属的孤儿保留并报告。不读取 Release 正文，不扩大原有 GC 范围与保留期。

Stable 聚合发布后,该版本线进入 7 天回退窗口。`Preview GC` 从 Stable Tag 可达的
`daily-preview.yaml` first-parent 历史生成精确 allowlist,不使用宽泛版本 glob。它还会
保护:

- 其他保留聚合 Tag 所引用的组件 Preview，包括无 Release 的 Tag 和 draft;
- `main` 和所有平台维护分支当前 Daily 清单引用的组件 Preview;
- 正在运行发布或删除工作流的对象。

计划阶段验证 Stable Release、Tag、声明资产 digest、canonical 清单、Preview 归属、
Release ID、可恢复源码 SHA 和 active run,并输出本次临时的稳定排序计划及摘要；包统计忽略旧校验和附件。正式版
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
重建已经删除的对象。组件和聚合删除在本轮 GC 调用中各最多派发三次当前 main 工作流，历史失败次数不会锁死后续运行；每次调用都重新计算 canonical 历史与实际引用。若一次成功清理后同名对象仍可见或被重新创建,下一次 Apply 会派发
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

Workbench Preview 清理遵守同样的 canonical 版本/源码归属检查。删除自有 package version 前验证仓库关联、不可变 digest 和源码/版本 label，并保护活跃发布、实际非目标 Tag，以及保留 index 引用的子 manifest，包括无 tag index。目标 index 的子镜像仅在本次观察到的引用闭包内考虑删除；无关或归属不明的镜像保留。

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
- 发布器不覆盖已发布 Tag 或不同内容资产。成功 stage 仍在时恢复原发布步骤；已公开且一致的 Release 只读核对。过期或冲突的 stage 须经受保护入口明确恢复;
- 分支 HEAD、Tag commit、清单 blob SHA 和发行资产验证共同固定一次发布;
- 即使渲染后的 Daily 清单无需写入,协调器仍复核远端分支 HEAD 与清单 blob,不会用陈旧
  checkout 派发组件;
- 新构建固定本次依赖闭包；已发布组件按 Tag 复用，不做历史绑定资格审核，由最终组合验收检查所选成品;
- scanner 使用独立 concurrency key；平台分支协调器与 GC 共用 `preview-manifest-selection-and-gc`。完整构建／发布与删除工作流按精确版本共用 mutation group。取消表示仍需处理，不等于发布成功。保留的成功 stage 优先恢复发布，其余安全重试采用上述当前调用预算。组件 publish 另用全仓排队，避免不同版本并发更新 Latest。从平台 main 准入的 Stable 聚合请求更新 Latest；发布器读回 Release 状态和资产身份。


## 11. 受保护分支

必需的 `ci / finalize` 检查必须对应当前已准入源码集并通过。平台同仓 PR 使用只读 reusable workflow 的 finalizer；组件和 fork 调用方保留受信 admission/status finalize，见 [CI 验收契约](ci_zh.md#1-公开调用方与受信任控制)。

已准入 lane 选择的所有检查必须成功。常规 x86/ARM 产品 E2E job 只有在可信 `docs`、`manifest`、`tooling` 或 `source` lane 的资格及精确证据通过后，才可按该 lane 有意跳过。必需工作失败、取消或非预期跳过均不能产生验收；Draft 的控制 job 成功不能代替资格验证。不能用结果桥接或独立提供的成功状态替代。

项目仓的分支保护以一个 repository ruleset 为准。当前已启用的 ruleset 只匹配
`refs/heads/main` 与 `refs/heads/release/v*`。它要求 PR、所有讨论已解决、严格匹配
`ci / finalize`、线性历史,并阻止 force push 与分支删除。不使用管理员默认绕过、签名
提交、CODEOWNERS 或 merge queue。

当前 `required_approving_review_count` 为 0。GitHub 只计入拥有 write 权限且不是 PR 作者的
approval;仓库没有独立的 write reviewer 时,强制一个 approval 会让维护者自己的所有 PR
无法按 ruleset 合入。检视仍要求完整 diff 检查、resolved review threads，以及已准入 CI lane 要求的精确当前源码证据。

启用前必须确认 `kuasar-sandbox-bms-ci` 的安装已获批 `Contents: write`。未满足此条件时
不得激活 ruleset: Daily Preview 无法提交收敛后的清单,会被唯一的 bypass 设计反向阻断。

状态检查规则对新建 branch 使用 `do_not_enforce_on_create: true`,因此可以从已经发布的
Stable Tag 建立一条新的维护线;创建后的任何更新立即回到同一套 PR 与必需 CI 门禁，不能借此
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
