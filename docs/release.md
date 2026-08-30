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
关闭:禁止新建 Preview,也禁止重复发布 Stable。

## 3. 分支与版本线

平台 `main` 发布最新主线版本。维护补丁使用平台 `release/vMAJOR.MINOR.x`。例如主线发布
`release-v0.1.0` 后,可以从该 Tag 建立 `release/v0.1.x`;维护分支发布
`release-v0.1.1`、`release-v0.1.2`,主线随后可以发布 `release-v0.2.0`。

若 `v0.2.0` 尚未规划而 `v0.1.0` 立即需要补丁,允许先在 `main` 发布 `v0.1.1`、
`v0.1.2`,再从较晚的补丁点建立 `release/v0.1.x`。建立维护分支之后,主线与维护线各自
维护自己的两个清单。

GitHub Latest 只由平台 `main` 的 Stable 发布更新。维护分支 Stable、任何 Preview 都不
更新 Latest。

平台维护分支与组件维护分支不存在同名约束。平台 `release/v0.5.x` 可以聚合
`sandboxer release/v0.3.x`、`orchestrator release/v0.4.x` 和只在 `main` 发布的
vmlinux 固定版本。

## 4. Daily 分支扫描与组件选择

`Daily Preview Scanner` 每天按 Asia/Shanghai 时区定时扫描:

- 平台 `main`;
- 所有符合 `release/vMAJOR.MINOR.x` 的平台分支。

每个分支使用独立 concurrency key。scanner 固定分支 HEAD SHA,再从平台 `main` 加载受
信任的控制器处理该 SHA。分支在选择或提交清单期间移动时,本次不覆盖远端状态,下一次从
新 HEAD 重扫。

### 4.1 源分支映射

- 平台 `main` 总是选择每个组件仓的 `main`;
- 平台维护分支从 Daily 清单中该 unit 的版本派生组件分支。`v0.3.5`、
  `v0.3.6-preview.*` 都映射到组件 `release/v0.3.x`;
- runtime 与 vmlinux 分别从 `runtime-vX.Y.Z`、`vmlinux-vX.Y.Z` 派生
  `release/vX.Y.x`;
- 派生分支不存在时,该 unit 固定复用清单指定的完整 Release,不自动修改版本。

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

## 5. 残缺发布恢复

完整组件 Release 必须是非 draft、prerelease 状态与 Tag 一致,只包含约定 archive 和
`SHA256SUMS`,且两个资产都处于 uploaded 状态。完整聚合 Release 必须满足八资产契约。

选择器仅把完整 Release 的 Tag 当作候选。残缺状态不让整条 Daily schedule 失败:

- 匹配的发布 run 仍在 queued/in progress:延后并在后续扫描恢复;
- 当前 Daily 清单拥有的残缺 Preview:先调用组件本仓的删除工作流,核对精确源码 SHA 后
  删除 Release、资产和 Tag,再重扫并重新走正常 Daily 发布;
- 外来残缺 Preview 或残缺 Stable:忽略,不作为候选,也不自动删除;
- 无组件维护分支的固定版本不完整:延后,不派生替代版本;
- 确定性失败最多重跑三次,超过后保持 deferred,不会靠覆盖 Tag 或手工拼资产恢复。

恢复的目标是恢复 Daily 流程,不是修补或重建残缺 Release 的资产。同名完整 Release 永远
不会被 `incomplete` 模式删除。若 Tag 已经缺失但 draft 或 prerelease 仍在,只有
Release 的 `target_commitish` 是完整且匹配的源码 SHA 时才允许恢复删除。

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
publish concurrency lock 后各执行一次。

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
但不抢占主线 Latest。

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

Apply 先 dispatch 五个组件仓的本地删除 wrapper。每个 wrapper 仅使用本仓短期
`GITHUB_TOKEN contents:write`,再次核对精确 Preview Tag 和源码 SHA,然后将 Release、
所有资产与 Tag 一并删除。所有组件候选消失后,平台才删除聚合 Preview。中途失败不会
重建已经删除的对象;下一次根据 canonical 历史重算并继续收敛。Stable Release、Stable
Tag、Actions artifacts、非 canonical orphan 都不属于 GC 删除集合。

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
- 组件发布、组件删除、聚合发布和平台删除只使用各仓本次 workflow 的短期
  `GITHUB_TOKEN contents:write`;
- 执行候选代码的 runner 只接收源码只读 token,并在执行前撤销;
- 发布先创建 draft、上传并复核 digest,全部一致后才公开;
- 已发布 Tag 或 Release 不由发布器覆盖;残缺 Preview 只能经受保护删除入口恢复;
- 分支 HEAD、Tag commit、清单 blob SHA 和 exact-asset BMS 共同固定一次发布;
- scanner 和每条平台分支使用各自的 concurrency key;同一组件发布单元的 publish 与
  delete 共用 `queue: max` 变更队列,平台聚合 publish 与 delete 也共用对应队列。队列
  保留最多 100 个 pending 任务且不取消运行中任务,避免 GC 或恢复请求替换其他待发布
  版本。

## 11. See Also

- [ci.md](ci.md):BMS revision、缓存和执行模式;
- [deployment.md](deployment.md):部署与运行前置条件;
- [../test/QUICKSTART.md](../test/QUICKSTART.md):完整聚合 Release 验证;
- [../release/](../release/):选择、打包、协调、恢复和 GC 实现。
