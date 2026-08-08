# Release

## 1. 概述

Kuasar Sandbox 把组件发布与平台聚合发布分开。组件版本回答某个组件仓交付了什么,
聚合版本回答哪些已发布组件作为一个平台组合完成了精确制品验证。`platform` 不建立独立
`vX.Y.Z` 版本线,platform 文档与测试包只作为 `release-vX.Y.Z` 的一个资产。

版本线为:

- `accelerator`、`connector`、`sandboxer`、`orchestrator`: `vX.Y.Z`;
- `guest-runtime` runtime: `runtime-vX.Y.Z`;
- `guest-runtime` kernel: `vmlinux-vX.Y.Z`;
- `platform`: `release-vX.Y.Z`。

所有版本线都接受 `-preview.YYYYMMDD` 后缀。preview 是 GitHub prerelease,不更新
Latest;正式版本使用新的 tag 和 workflow run,不重命名或覆盖 preview。

## 2. CLI

### 2.1 发布组件

组件 workflow 只接受显式 `workflow_dispatch(version)`,没有独立 schedule:

```bash
gh workflow run release.yml \
  --repo kuasar-sandbox/accelerator --ref main -f version=v0.2.0

gh workflow run component-release.yml \
  --repo kuasar-sandbox/orchestrator --ref main -f version=v0.1.5

gh workflow run release-runtime.yml \
  --repo kuasar-sandbox/guest-runtime --ref main \
  -f version=runtime-v0.2.1 -f sandboxer_version=v0.2.0

gh workflow run release-vmlinux.yml \
  --repo kuasar-sandbox/guest-runtime --ref main -f version=vmlinux-v0.1.4
```

`connector`、`sandboxer` 与 `accelerator` 均使用 `release.yml`。每个 run 固定 dispatch
时的组件 `main` commit,依次完成构建、测试、打包和发布。已发布的同名 Release 或指向
其他 commit 的同名 tag 都会失败,不会覆盖。

### 2.2 发布聚合版本

正式版本先在 `platform/main` 增加 `releases/release-vX.Y.Z.yaml`,然后运行:

```bash
make -C platform release RELEASE_VERSION=release-v0.2.1
```

该入口复用已发布组件,触发缺失的组件 workflow,等待 runtime 的 sandboxer 前置版本,
最后触发 platform `Aggregate Release`。若六个组件已经发布,可以直接运行:

```bash
gh workflow run aggregate-release.yml \
  --repo kuasar-sandbox/platform --ref main \
  -f version=release-v0.2.1
```

### 2.3 本地验证发布工具

```bash
make -C platform test-release-tools
```

该测试建立六个本地组件 fixture,验证版本选择、固定资产名、platform 包、组件原包复制、
统一 `SHA256SUMS`、安全解压、篡改拒绝、draft 重试和 prerelease 状态,不访问远程仓库。

## 3. 配置

### 3.1 聚合版本选择

正式和 preview 聚合版本选择都是 `platform/main` 中的普通源码配置:

```yaml
version: release-v0.2.1
previous: release-v0.2.0
components:
  accelerator: v0.2.0
  connector: v0.1.3
  sandboxer: v0.2.0
  orchestrator: v0.1.5
  runtime: runtime-v0.2.1
  vmlinux: vmlinux-v0.1.4
```

文件路径为 `releases/release-v0.2.1.yaml`。`previous` 显式指定用于生成更新说明的上一
聚合版本;聚合时会同时解析当前清单和它直接引用的上一清单。解析器要求六个 release unit
恰好各出现一次,
并分别校验版本前缀。文件不声明架构:架构是资产维度,同一个聚合版本必须包含平台当前支持
的全部目标。当前完整构建和 BMS 只覆盖 Linux x86_64,因此当前 Release 只发布该目标。

每日协调器同样先生成 `releases/release-vX.Y.Z-preview.YYYYMMDD.yaml`,通过 GitHub Contents
API 将它作为普通提交写入 `platform/main`,再触发任何组件或聚合 workflow。清单一旦提交就
冻结本次选择,恢复任务只重用它,不会重新计算。清单只在代码库维护:不作为 GitHub Release
资产发布,不写入 platform archive,也不复制进聚合 bundle。

### 3.2 GitHub App

源码 BMS 和聚合资产下载使用只读 App:

- variable `KUASAR_CI_APP_CLIENT_ID`;
- secret `KUASAR_CI_APP_PRIVATE_KEY`;
- 六仓 `Contents: read`。

每日协调器使用独立 release-controller App:

- variable `KUASAR_RELEASE_APP_CLIENT_ID`;
- secret `KUASAR_RELEASE_APP_PRIVATE_KEY`;
- 六仓 `Actions: write` 与 `Contents: read`。

release-controller token 只存在于 GitHub-hosted 协调 job。自托管 BMS 不接收仓库写 token;
源码只读 token 在执行候选代码前显式撤销。组件和聚合 publish job 只使用当前仓的短期
`GITHUB_TOKEN` 和 `contents:write`。每日协调 job 仅用 platform 自身的短期 `GITHUB_TOKEN`
把生成的版本选择清单提交到 `platform/main`,release-controller App 不获得内容写权限。

## 4. 组件资产契约

当前 x86_64 普通组件 Release 的显式资产只有:

```text
<component>-vX.Y.Z-linux-x86_64.tar.gz
SHA256SUMS
```

archive 只包含实际交付的 `bin/`、`docs/`、可选 `deploy/` 和可选 `test/`。不上传项目生成
的 release metadata JSON,archive 内也没有 `release/*.json`。源码由 GitHub 根据 tag
自动提供的 zip/tar.gz 表达,工作流不额外上传,聚合 Release 也不复制。

publish job 不读取 manifest。它从 workflow version、目标和固定命名规则计算预期资产,
校验包内必需路径、禁止路径、权限、`SHA256SUMS` 以及 Go build info,随后创建 draft,
上传两个资产并用 GitHub API 对比名称、大小与 `sha256:` digest。全部一致后才发布 draft。

Go 二进制使用标准 `go version -m` 检查。通过兄弟仓本地 `replace` 构建的模块可能显示
`(devel)`,这是当前接受的构建边界;发布流程不维护另一份跨仓 revision 图。

## 5. Guest runtime 资产

tag 前缀保持不变,资产名不重复 tag 已表达的类型前缀:

```text
runtime-v0.1.0-preview.20260808
  -> sandbox-runtime-x86_64-v0.1.0-preview.20260808.tar.gz

vmlinux-v0.1.0-preview.20260808
  -> vmlinux-x86_64-v0.1.0-preview.20260808.tar.gz
```

runtime archive 只有一个稳定入口 `bin/sandbox-runtime.bundle`,并同时交付 `flatten-ctl`、
`mkfs.erofs`、文档和 flatten E2E。vmlinux archive 只有 `bin/vmlinux` 与内核文档。
两个包都不再保留内容相同的版本化文件副本。

runtime workflow 显式选择一个已经发布的 sandboxer tag,因为其 `sandbox-init` 会进入
runtime image。聚合可选择另一个 sandboxer 版本;组合兼容性由 exact-asset BMS 判断。

## 6. Platform 聚合资产

当前 `release-vX.Y.Z` 精确包含八个显式资产:

```text
platform-release-vX.Y.Z.tar.gz
accelerator-<selected>-linux-x86_64.tar.gz
connector-<selected>-linux-x86_64.tar.gz
sandboxer-<selected>-linux-x86_64.tar.gz
orchestrator-<selected>-linux-x86_64.tar.gz
sandbox-runtime-x86_64-<selected-without-runtime-prefix>.tar.gz
vmlinux-x86_64-<selected-without-vmlinux-prefix>.tar.gz
SHA256SUMS
```

六个组件 archive 从组件 Release 按字节复制,不改名、不重压缩。聚合层丢弃各组件独立
`SHA256SUMS`,生成覆盖 platform 包与六个 archive 的统一校验文件。Release notes 列出所选
组件 tag,并按清单中的 `previous` 汇总六个组件的 commit 更新清单,但不作为机器清单。

platform 包使用确定性 tar 规则,只包含 `docs/` 与可交付 `test/`;不包含组件二进制、版本
选择、workflow、runner 配置、cache 或凭据。它没有独立 tag 或独立 Release。

## 7. 聚合与 exact-asset BMS

`Aggregate Release` 在 GitHub-hosted prepare job 中完成:

1. 从当前 platform commit 解析版本选择;
2. 查询六个已发布、非 draft 的组件 Release;
3. 要求每个 Release 的显式资产精确等于固定 archive 与 `SHA256SUMS`;
4. 下载并同时验证 GitHub size/digest 和组件 SHA-256;
5. 生成确定性 platform 包并按字节复制六个 archive;
6. 拒绝 archive 中的不安全路径和跨包文件覆盖;
7. 生成统一 `SHA256SUMS`,上传为当前 workflow 的短期 artifact。

可复用 BMS 在自托管 KVM runner 下载同一个 artifact,重新验证后把七个 archive 解压到
一个安装目录。测试入口是即将发布的 platform 包中的 `test/e2e/run_all.sh`;组件包携带的
模块 E2E 也合并到该安装目录。该模式不 checkout 组件 main、不调用 native cache、不重新
构建组件或 Linux kernel。

BMS 成功后,GitHub-hosted publish job 下载同一个 artifact,以本次 workflow 的
`github.sha` 创建 `release-vX.Y.Z` tag,验证上传后的八个资产后发布 draft。tag 已存在但
指向另一个 commit 时立即失败。

## 8. 每日 preview

platform 只保留两个 schedule:

- 02:13 Asia/Shanghai:主协调;
- 05:13 Asia/Shanghai:恢复协调。

协调器首先比较每个 release unit 的最新已发布 tag 与仓库 `main`。如果该 unit 自上次发布
以来有新提交,就在当日清单中选择新的 preview tag;没有新提交时,复用其最新 preview 或正式
tag。runtime 与 vmlinux 是 `guest-runtime` 仓的两个独立 release unit,各自寻找本版本线的
最新 tag 并与同一个仓库 `main` 比较。选择结果和显式 `previous` 先提交到 `platform/main`。

清单冻结后,协调器并行收敛 accelerator、connector、sandboxer、orchestrator 和 vmlinux;
sandboxer 发布后再收敛 runtime;六个组件完成后触发 aggregate。每一步根据 Release 和
workflow run 状态幂等处理:

- Release 完整存在:复用;
- run queued/in progress:继续等待;
- runner 离线、超时、网络错误等基础设施失败:最多复跑同一 run 两次,保留原 `GITHUB_SHA`;
- 构建、测试、资产或 BMS 的确定性失败:停止并报告,不无限重试;
- 从未 dispatch:只创建一次 workflow dispatch;
- aggregate Release 已存在:成功结束。

恢复入口扫描代码库中已有但尚未形成 platform Release 的顶层 preview 清单,找到最早日期,
因此跨日 pending 不会因当天日期变化而遗失,也不会从临时 workflow 状态重建选择。任一正式
组件已经发布但正式聚合尚未完成时,每日 preview 暂停,避免正式发布窗口继续创建新 preview;
正式聚合发布后 daily 直接成功退出。

## 9. 可靠性与安全

- 组件、platform 包和聚合包先在本地校验,再创建 draft;
- 失败的 draft 可按 Release ID 删除并重建,已发布 Release 不可替换;
- 同版本 workflow concurrency 不取消正在发布的 run;
- 所有外部下载使用有限重试、连接超时和总超时;
- BMS 公共实现只在 platform 维护,组件 wrapper 以完整 platform commit SHA 引用;
- reusable workflow 用 `job.workflow_repository` 和 `job.workflow_sha` checkout 自身工具;
- fork 候选必须由可信 wrapper dispatch 当前 two-parent integration commit;
- 候选 commit、PR base/head 和当前 merge commit 不一致时拒绝执行;
- 自托管 job 没有 `contents:write` 或 release-controller token。

## 10. See Also

- [ci.md](ci.md):BMS revision、缓存与两种执行模式;
- [deployment.md](deployment.md):平台部署与运行前置条件;
- [../test/QUICKSTART.md](../test/QUICKSTART.md):发布包验证和 E2E 操作;
- [../release/](../release/):版本选择、打包、发布与协调器实现。
