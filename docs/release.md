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
接受 `-preview.YYYYMMDD` 后缀。Preview 是 GitHub prerelease,Stable 是非 prerelease;
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

### 2.1 Stable 清单

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

Stable `V1` 发布后,如果继续在同一分支开发,必须先将 Daily 清单推进为 `V2`,并设置
`previous_version: V1`。准备 `V2` 时再推进 Stable 清单。`V2` 发布后两个清单继续推进
到 `V3`,以此类推。Daily 与 Stable 清单版本相等且该 Stable 聚合已经发布时,该版本线
关闭:禁止新建 Preview,也禁止重复发布 Stable。若同版本 Stable Release 已出现但资产或
Tag 尚不完整,Daily 同样在任何清单或组件变更前延后,不在残缺 Stable 旁继续发布 Preview。

## 3. 分支与版本线

平台 `main` 发布最新主线版本。维护补丁使用平台 `release/vMAJOR.MINOR.x`。例如主线发布
`release-v0.1.0` 后,可以从该 Tag 建立 `release/v0.1.x`;维护分支发布
`release-v0.1.1`、`release-v0.1.2`,主线随后可以发布 `release-v0.2.0`。

若 `v0.2.0` 尚未规划而 `v0.1.0` 立即需要补丁,允许先在 `main` 发布 `v0.1.1`、
`v0.1.2`,再从较晚的补丁点建立 `release/v0.1.x`。建立维护分支之后,主线与维护线各自
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
控制器使用 GitHub App 的只读 contents Token 访问私有组件仓;Token 只通过子进程级 Git
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

获胜 Tag 已在 HEAD 时直接复用。获胜 Stable Tag 落后于 HEAD 时,Patch 只自增一次并产生
`vX.Y.(Z+1)-preview.YYYYMMDD`;获胜 Preview 落后于 HEAD 时保持其 core 版本,只换成
本次日期。下一次扫描会看到这个 Preview,不会反复增加 Patch。

依赖改变也需要重建实际链接依赖的 unit:accelerator 或 connector 改变会重建
sandboxer、orchestrator 和 runtime;sandboxer 改变会重建 orchestrator 和 runtime。
vmlinux 不因这些依赖变化而重建。若同一 unit 在同一天已经发布 Preview 后依赖再次改变,
流程延后到下一个 Preview 日期,不复用带有旧依赖的资产,也不发明同日序号格式。

平台自身在所选分支上的代码变化同样会产生新的聚合 Preview,即使六个组件都复用。
同一聚合版本的 workflow run 名同时绑定平台源码 SHA。只要任意匹配 run 仍在运行,即使
更新的 run 已取消或结束,控制器也保持当前 Daily 清单不变;分支产生新 HEAD 后使用新的
run 身份,不会用旧输入重跑或改写运行中清单。
分支协调任务的身份还包含请求日期,因此相同平台 SHA 的不同日期扫描不会错误复用彼此的
结果。

## 5. 残缺发布恢复

完整组件 Release 必须是非 draft、prerelease 状态与 Tag 一致,只包含约定 archive 和
`SHA256SUMS`,且两个资产都处于 uploaded 状态。完整聚合 Release 必须满足八资产契约。

选择器仅把完整 Release 的 Tag 当作候选。残缺状态不让整条 Daily schedule 失败:

- 匹配的发布 run 仍在 queued/in progress:延后并在后续扫描恢复;
- 当前 Daily 清单拥有的残缺 Preview:先调用组件本仓的删除工作流,核对精确源码 SHA 后
  删除 Release、资产和 Tag,再重扫并重新走正常 Daily 发布;
- 外来残缺 Preview 或残缺 Stable:忽略,不作为候选,也不自动删除;
- 无组件维护分支的固定版本不完整:延后,不派生替代版本;
- 确定性失败最多重跑三次,超过后保持 deferred,不会靠覆盖 Tag 或手工拼资产恢复。普通
  failure 只重跑失败 job;cancelled、timed out 等没有失败 job 的状态重跑完整 workflow。

若未完成的聚合选择跨过了新的上海日期,控制器不再用旧日期 Tag 绑定新的组件 HEAD。
残缺聚合对象先通过受保护入口删除;随后清单滚到新日期并重新选择。旧选择中已经完整发布
但未被任何保留聚合引用的组件 Preview 交给 GC 按回退窗口统一删除。若仍在同一天,控制器
保持 deferred,避免覆盖已经公开的完整 Preview Tag。

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

orchestrator 和 runtime 还接收 `sandboxer_version`;runtime 与 vmlinux 使用各自工作流。
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

本地发布工具验证入口为:

```bash
make test-release-tools
make test-ci-tools
```

## 8. 资产与 BMS

每个普通组件 Release 精确包含组件 archive 和 `SHA256SUMS`。runtime、vmlinux 使用各自
独立 archive 名。聚合 Release 精确包含 platform archive、六个原样复制的组件 archive
和统一 `SHA256SUMS`,共八项显式资产。

组件工作流在选定源码 SHA 上运行组件构建和测试。聚合 prepare 下载清单指定的六个完整
Release,校验 GitHub size/digest、组件 SHA-256、包内路径与跨包覆盖,生成确定性 platform
包。随后 exact-asset BMS 在真实 KVM runner 解压同一个短期 artifact,执行六个 owner
套件及平台组合套件。只有该 BMS 成功,聚合 publish job 才能创建 Tag 和 Release。

Preview、维护分支 Stable 和主线 Stable 使用相同资产与 BMS 门禁;差别只在发行状态与
Latest 策略。

## 9. Preview GC

Stable 聚合发布后,该版本线进入 7 天回退窗口。`Preview GC` 从 Stable Tag 可达的
`daily-preview.yaml` first-parent 历史生成精确 allowlist,不使用宽泛版本 glob。它还会
保护:

- 任何其他仍保留的聚合 Preview 所引用的组件 Preview;
- `main` 和所有平台维护分支当前 Daily 清单引用的组件 Preview;
- 正在运行发布或删除工作流的对象。

计划阶段验证 Stable Release、Tag、八资产 digest、canonical 清单、Preview 归属、
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

```bash
# 只读真实计划
gh workflow run preview-gc.yml --repo kuasar-sandbox/kuasar-sandbox --ref main \
  -f stable_version=release-v0.5.7 -f dry_run=true

# 到达 7 天窗口后收敛
gh workflow run preview-gc.yml --repo kuasar-sandbox/kuasar-sandbox --ref main \
  -f stable_version=release-v0.5.7 -f dry_run=false
```

## 10. 权限与可靠性

- 跨仓 GitHub App token 只有 `Contents: read` 和 `Actions: write`;
- Daily 清单提交使用同一个 App 仅面向 `kuasar-sandbox` 仓的独立短期
  `Contents: write` token;它不具有其他组件仓写入权,也不复用通用
  `github-actions` 身份;
- 组件发布、组件删除、聚合发布和平台删除只使用各仓本次 workflow 的短期
  `GITHUB_TOKEN contents:write`;
- 执行候选代码的 runner 只接收源码只读 token,并在执行前撤销;
- 发布先创建 draft、上传并复核 digest,全部一致后才公开;
- 已发布 Tag 或 Release 不由发布器覆盖;残缺 Preview 只能经受保护删除入口恢复;
- 分支 HEAD、Tag commit、清单 blob SHA 和 exact-asset BMS 共同固定一次发布;
- 即使渲染后的 Daily 清单无需写入,协调器仍复核远端分支 HEAD 与清单 blob,不会用陈旧
  checkout 派发组件;
- Preview Release 的构建绑定防止相同 Tag/源码在不同依赖闭包之间被错误复用;
- scanner 使用独立 concurrency key;所有平台分支协调器与 GC 共用全局
  `preview-manifest-selection-and-gc` concurrency group。scanner 顺序等待每个分支,不同时
  填入多个 pending slot。组件完整构建/发布 workflow 与 delete 对同一精确版本共用
  mutation group;平台聚合完整 prepare/BMS/publish workflow 与 delete 也使用同一精确
  版本组。GitHub 可能合并该组内的 pending 请求;Daily 协调器和 GC 不把 cancelled 当作
  成功,而是按同一精确输入重跑完整 workflow,最多三次后才 deferred。因此互斥不会把
  发布或删除的目标状态静默丢失。不同版本不共享 pending slot。组件 Latest 协调器是
  例外:其操作幂等且每次都
  扫描完整主线 Stable 集合,因此使用全仓串行组并允许多个触发合并;保留下来的最后一次
  运行仍能收敛完整状态。只有
  当前平台 `main` HEAD 的 `release.yaml` 所选 Stable 聚合能更新 Latest,且发布前后都会
  重新验证分支 HEAD、清单选择和既有 Release。

## 11. 受保护分支

项目仓的分支保护以一个 repository ruleset 为准。启用该 ruleset 时,它只匹配
`refs/heads/main` 与 `refs/heads/release/v*`。它要求 PR、所有讨论已解决、严格匹配
`bms / finalize`、线性历史,并阻止 force push 与分支删除。不使用管理员默认绕过、签名
提交、CODEOWNERS 或 merge queue。

当前 `required_approving_review_count` 为 0。GitHub 只计入拥有 write 权限且不是 PR 作者的
approval;仓库没有独立的 write reviewer 时,强制一个 approval 会让维护者自己的所有 PR
无法按 ruleset 合入。代码检视仍通过完整 diff 自检、resolved review threads 和精确 BMS
门禁执行。

启用前必须确认 `kuasar-sandbox-bms-ci` 的安装已获批 `Contents: write`。未满足此条件时
不得激活 ruleset: Daily Preview 无法提交收敛后的清单,会被唯一的 bypass 设计反向阻断。

状态检查规则对新建 branch 使用 `do_not_enforce_on_create: true`,因此可以从已经发布的
Stable Tag 建立一条新的维护线;创建后的任何更新立即回到同一套 PR 与 BMS 门禁，不能借此
绕过后续提交检查。

Daily Preview 必须直接把收敛后的清单提交到受保护目标分支,因此启用后的 ruleset 只为
`kuasar-sandbox-bms-ci` GitHub App (ID `4283831`) 配置 `always` bypass。该 App 的唯一写入
用途是上述本仓短期 token;人工维护、普通 `github-actions` 与所有其他 App 都不在 bypass
列表。BMS 对 PR 始终重新验证精确 integration commit 与目标 branch,所以 bypass 不替代
`bms / finalize` 门禁。

## 12. See Also

- [ci.md](ci.md):BMS revision、缓存和执行模式;
- [deployment.md](deployment.md):部署与运行前置条件;
- [../test/QUICKSTART.md](../test/QUICKSTART.md):完整聚合 Release 验证;
- [../release/](../release/):选择、打包、协调、恢复和 GC 实现。
