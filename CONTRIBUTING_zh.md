[English](CONTRIBUTING.md) | [简体中文](CONTRIBUTING_zh.md)

# 贡献指南

感谢为 Kuasar Sandbox 项目主仓 `kuasar-sandbox/kuasar-sandbox` 贡献。

## 贡献许可

提交贡献即表示同意按照所修改文件适用的许可证授权。没有另行明确声明许可证的新项目文件采用 [Apache License 2.0](LICENSE)。

只提交你有权贡献的内容。修改第三方或采用其他许可证的材料时，保留适用的版权、署名、NOTICE 和 SPDX 声明。

## Pull Request

每个 PR 保持范围聚焦，有相关 Issue 时请关联，并说明已执行的验证。不得包含凭据、私有数据或无关的生成文件。

## CI 准入与外部贡献

任何人都可以创建和讨论公开 Fork PR。当前自动 Integration E2E 接纳 Ready 的同仓 PR，以及 **PR 作者当前为组织活跃成员**的 Fork PR。成员身份以实时查询为准；`author_association`、评审批准或维护者点击 **Re-run** 都不能让非成员 Fork 自动取得准入资格。主仓框架 PR 保留现有只读自验证路径。精确候选校验见 [CI 契约](docs/ci_zh.md)。

维护者可以通过以下方式接收外部贡献，无需授予贡献者组织成员身份或暴露控制凭据：

1. 记录原始 PR、目标分支及准备接收的精确提交。从所属上游仓库获取公开 PR head，检查完整差异，包括工作流、构建脚本和依赖修改。不要在带有维护者凭据或共享特权环境中执行未经检查的 checkout。
2. 检查完成后，从当前目标分支创建聚焦的组织仓分支。使用 `git cherry-pick -x` 或等价的保留作者归属方式，仅导入已检查的提交；保留原作者，在新 PR 中链接原始 PR 和来源提交。不覆盖现有分支，不向候选复制凭据。
3. 让接收后的 PR 执行正常源码/制品检查，并按仓库规则合入。检查最终差异和当前 head/base 的实际结果；接收动作不等于验证，也不会产生成功状态。
4. 在原始 PR 回填接收 PR 和处理结果。后续外部提交必须重新检查并形成新的已验证候选，不因先前接收自动纳入。

此流程不要求特定评审机器人、额外批准票数或特定 formal review 格式，也不授权绕过仓库规则。控制、App 和发布凭据仍不得进入候选执行 job。

<a id="source-workspace"></a>
## 源码工作区

标准本地工作区由六个兄弟仓组成:

| 工作区目录 | 职责 |
|---|---|
| `kuasar-sandbox/` | 平台集成、测试、发行协调与共享文档。 |
| `accelerator/` | 制品与数据基础能力。 |
| `connector/` | 沙箱网络与 FD 交接。 |
| `sandboxer/` | MicroVM 生命周期与资源机制。 |
| `guest-runtime/` | Guest runtime bundle、内核与镜像 CLI 交付。 |
| `orchestrator/` | 托管沙箱、节点与集群服务。 |

主仓主要目录:

```text
docs/             System design, deployment, performance, CI, and release docs
test/e2e/         公共 runner、扁平 suite 用例和预备输入 helper
test/perf|demo/   Platform performance and demo scripts
ci/integration/   CI helpers and source-cache maintenance
ci/native-cache/  vmlinux, erofs, envd, RocksDB, and Cloud Hypervisor cache
ci/runner/        Self-hosted CI runner deployment
release/          Version resolution, packaging, aggregation, and publishing
releases/         Stable and daily Preview aggregate selections
.github/workflows/Reusable CI, aggregate release, and daily Preview workflows
```

<a id="build-and-test"></a>
## 构建与测试

构建使用环境提供的 Go，并继承 `GOROOT`、`GOTOOLCHAIN` 等工具链选择；发布自动化需要环境在 `PATH` 中提供可用的 `gh`。项目不下载、替换或按固定二进制摘要认证这些环境工具。 EROFS 工具继续按项目源码、补丁及配方构建。

完整源码构建从主仓驱动五个组件仓,并按
[`release/inputs/bin-inputs.yaml`](release/inputs/bin-inputs.yaml) 把运行文件收集到
`bin/<arch>/`:

```bash
make -C kuasar-sandbox build
make -C kuasar-sandbox test
make -C kuasar-sandbox test-e2e RELEASE_DIR=/release E2E_WORKDIR=/tmp/kuasar-prepared
make -C kuasar-sandbox perf E2E_WORKDIR=/absolute/path/to/prepared-inputs
make -C kuasar-sandbox demo PYTHON_BIN=/absolute/path/to/pinned-venv/bin/python
```

单仓构建仍从组件仓执行.例如:

```bash
GOWORK=off make -C sandboxer build
```

### 从源码构建本地 node-infra 镜像

在原生 Linux 主机（x86_64 或 aarch64）上，按[源码工作区](#source-workspace)的布局准备主仓和五个组件仓。六个仓库都应是干净的检出，有本地改动时，先提交或另存。安装组件构建和打包所需工具、`skopeo`，以及 Podman（默认）或 Docker；先将 [Containerfile](node-infra/image/Containerfile) 指定的基础镜像载入所选引擎，例如 `podman pull docker.io/library/ubuntu:24.04`。构建镜像时还需能访问配置的 Ubuntu 软件源。组件打包会读取原生依赖源码树及其许可证材料，仅拷贝 `bin/<arch>/` 不够。

```bash
make -C kuasar-sandbox node-infra-image
# 如需改用 Docker 构建：
make -C kuasar-sandbox node-infra-image NODE_INFRA_ENGINE=docker
```

该目标会先执行平台构建，再将 OCI 归档、Docker 归档和构建记录写入 `kuasar-sandbox/build/node-infra/<arch>/` 下的新目录，不会发布镜像。使用 Docker 测试时，执行 `docker load -i <Docker归档路径>`，然后使用输出中的镜像标签 `localhost/node-infra:local-<hash>`。

### 验证构建

产品 E2E 通过 `test/e2e/e2e prepare` 和 `run` 消费预构建制品。完整的 `<suite>.<case>.sh` 文件名标识九个 suite 中的用例；组件归属只决定源码维护位置。prepare 获取不可变镜像与固定 Demo SDK；执行使用预备制品、helper 和镜像，不发现源码、不编译、不自动拉取。unit/race/vet、UFFD 和性能门禁保持独立。详见[测试组织](test/README_zh.md)和[发布验证指南](test/QUICKSTART_zh.md)。

本地发布工具测试不访问 GitHub,也不构建真实 native 依赖:

```bash
make -C kuasar-sandbox test-release-tools
make -C kuasar-sandbox test-ci-tools
make -C kuasar-sandbox test-perf-tools
```

需要 KVM、root、eBPF、systemd、外部存储或完整兄弟源码集的测试分别声明这些前置条件。
跳过特权用例不能解释为完成集成验收。

<a id="文档语言与检视规范"></a>
## 文档贡献

按完整的用户任务或组件契约组织文档。将单次修复说明并入所属规范；不再维护的一次性报告直接删除，不为其新建归档层级。仅在承担真正独立的责任时新增或拆分文档；功能、修复、package 或 flag 本身不足以支持独立文件。历史证据不要求保留一次性报告文件，可由原 Git/PR/Issue 历史保存。在 PR 中记录章节去向和删除的重复内容，同步保留中英文版本，并检查源码与发布包中的导航。

<a id="documentation-organization"></a>
### 组织方式与权威归属

先确定能力与读者任务，再确定责任归属，最后选择文件。
[架构归属表](docs/architecture_zh.md#配置与契约归属)与[文档目录](docs/README_zh.md)记录当前边界。

| 位置 | 职责 |
| --- | --- |
| 主仓 `docs/` | 平台概念/架构，以及完整的跨组件使用、部署、运维和升级任务 |
| 组件 `docs/` | 能力或对象契约：按需要组织用法、公开接口、精确配置、状态、失败边界与实现 |
| 根 README | 项目/组件范围、简短首次使用入口和主题导航 |
| CONTRIBUTING | 源码工作区、构建测试入口、贡献与评审流程 |
| `examples/`、`test/`、`native-deps/` README | 邻近可执行材料的步骤和前置条件 |
| `release/` 声明 | 交付文档选单与明确的导航兼容，参见[发行契约](docs/release_zh.md#带版本的导航迁移) |

独立文件需要完整任务或稳定对象/契约、明确读者与维护归属，以及与相邻主题的边界。
package、flag、进程、缺陷或篇幅本身不足以决定单列。Runtime 内存、块存储与资源控制
采用相同子系统尺度，具体实现作为章节。API 与 CLI 可共同解释同一个能力，无需形成
两份独立特性规范。配置语法和默认值只有一个归属，机制文档解释行为并链接完整 schema。

保持浅层 `docs/` 和稳定英文主题名。开头说明范围、前置条件和权威边界；按任务需要组织
概念、完整示例、精确参考、机制/失败与验证。使用语义标题和链接，避免再维护一套章节号索引。
相关链接放在正文之后。

精确状态转换、wire 版本、格式字段和默认值只在一个权威主题维护；指南保留可执行示例和
必要解释。结构修改需将每个原维护章节映射到目标，或依据证据说明其过时/重复原因。
退役路径不允许丢失规范或中文版。同步文档声明，并用当前和独立选取的历史组件输入检查
装配导航。文档改写不改变产品行为或发布策略。

### 范围

所有持续维护的第一方项目、设计、部署、构建、运维、验证和示例文档都必须提供完整英文内容。已有英文单语文档符合要求。翻译中文文档时，在英文默认文件旁保留完整中文版：`name.md` 与 `name_zh.md`。两个文件均在开头提供双向语言链接。不为满足语言规范而重建文档目录或复制上游法律文本。

### 翻译契约

翻译前必须读取记录了精确提交的完整源文档. 单纯翻译保留原有组织、章节编号、表格、图、示例、限制及失败行为.摘要或英文简介不能替代规范全文。长文档可以分提交检视，但翻译 PR 合入前默认文档必须完整。

除单独标识且有源码依据的订正外，API 与 CLI 名称、配置键和值、状态名、格式字段、常量、路径、单位及可执行示例保持不变。翻译说明性注释和图中文字时，不改变程序语义及图的关系。详细设计文档保留精简项目介绍中可能省略的协议与实现术语。

规范性要求不能在翻译中增强或削弱：MUST、MUST NOT、SHOULD、SHOULD NOT、MAY 分别保留原有强度。不能仅通过翻译，把某次实现观察变成普遍要求或兼容性承诺。

### 结构整理

经明确批准的结构整理允许退役旧文档路径、删除纯转发章节与迁移别名, 并重排章节编号. 完整的维护中契约必须保留在归属文档, 英中版本同步, 所有维护中的入站引用迁到实际主题. 旧 URL 和章节号不要求永久保留兼容页或别名. 上游法律文本、真实协议兼容边界和历史证据不受影响.

### 订正已有文档

以归属组件的实现、固定构建输入和已记录验证证据核对有争议的表述。在 PR 中将事实订正与翻译分开说明，并同步修改两种语言。来源无法证明的结论应明确不确定性或删除无依据承诺，不得填入杜撰事实。

区分已实现行为、设计提案与历史测量，保留历史报告的环境及版本。语言修改不实现产品功能、不重跑无关基准、不关闭安全机制、不改变发布策略。上游许可证及版权声明保持原样。

### 导航与版本

英文导航应指向英文默认路径；中文导航优先指向已存在的中文对应文件，否则标明目标仅有英文。更新相对链接、引用式链接、章节片段、显式锚点和编号引用。单纯翻译时尽可能保留已有外部引用的锚点. 经批准的结构整理按批准的退役范围迁移维护中的引用, 不要求保留旧路径或旧编号.

分别检查源码仓库和聚合文档：六仓工作区有效的路径不一定在归档中有效。不存在仓库内目标时，使用显式跨仓源码 URL。链接不得携带私有下载令牌。

记录原始提交、修改文件、实现 PR 和验证结果。合入前与目标分支最新来源比较，将期间产生的语义变化同步进两种语言。不能用旧基线副本覆盖更新后的中文内容。后续语义变更应在同一个 PR 中同步维护两种语言。

### 自动检查

无需联网即可运行仅依赖标准库的检查器：

```sh
python3 -m unittest discover -s ci -p 'test_check_docs.py'
: "${BASE_COMMIT:?Set the reviewed base commit SHA}"
python3 ci/check_docs.py --root . --changed-base "$BASE_COMMIT"
python3 ci/check_docs.py --root . --json
```

changed-base 模式只检查修改过的 Markdown 及其双向选择器，不把无关文档纳入聚焦修改的检查。全树模式报告所有待处理问题，是最终验收的必需项。聚焦检查不豁免其他持续维护文档的规范要求。

检查器覆盖普通 Markdown 内联及引用式目标、本地文件存在性、章节片段、配对、选择器及代码围栏外无意残留的中文。它不联网，也不能证明翻译语义完整。复杂 Markdown 与可执行示例需要另行检视。英文文档单行中确需中文时可使用 `<!-- docs:allow-han -->`，每个例外必须经过检视。用于 Unicode 验证的代码数据、语言选择器、上游许可证及未修改的第三方材料不属于漏译正文。

### 检视与完成

PR 记录 Summary、Corrections、Translation coverage、Validation 和 Out of scope。对照精确 head 与最新 base 检查最终差异、所有评审讨论和必需检查。遵循正常合入规则，不绕过管理员保护、不虚构评审批准。

最终验收分别记录逐文件覆盖、语义检视、链接、源码/归档布局及合入证据。语言扫描通过不代表翻译正确；未执行的测试不能计为通过。已有 Release 资产不可变,普通文档工作不能替换这些资产。
