[English](README.md) | [简体中文](README_zh.md)

# Kuasar Sandbox

Kuasar Sandbox 是一套支持生产部署的 MicroVM 沙箱平台,面向大规模 Agent,
Serverless 与强化学习工作负载,提供独立 Guest Kernel 隔离,快照模板实例化,
有状态暂停恢复,按需数据加载,高密资源治理,以及从单节点到多节点集群的完整能力.

系统支持生产部署.[Stable 通道](https://github.com/kuasar-sandbox/kuasar-sandbox/releases/latest)
解析为当前非 prerelease 聚合版本;一次安装应只解析一次 Release,并固定其精确 Tag、全部
资产和校验和.Preview 继续用于开发和评估.生产就绪描述系统的部署能力,公开发行通道标记描述资产
和接口的稳定性,两者是不同维度.生产部署仍应结合工作负载完成容量验证,
并配置正式 TLS,可靠存储,网络策略和安全凭据.

## 快速导航

- [快速开始](docs/quickstart_zh.md):从同一聚合 Release 下载资产,校验并用 E2B SDK 运行首个真实 MicroVM;
- [Architecture](docs/kuasar-sandbox_zh.md):系统能力,组件边界和关键语义;
- [Deployment](docs/deployment_zh.md):单节点,集群拓扑与进程依赖;
- [Releases](docs/release_zh.md):组件版本,聚合版本和资产契约;
- [Demo](test/demo/DEMO_zh.md):本地体验环境和 E2B SDK 演示;
- [Full validation](test/QUICKSTART_zh.md):完整 Aggregate Release E2E 验收;
- [安全策略](SECURITY_zh.md):支持范围和私密漏洞报告入口;
- [贡献指南](CONTRIBUTING_zh.md):项目仓贡献规则与组织级指南.

## 为什么使用 MicroVM 沙箱

Agent 会运行模型生成的命令、用户程序、第三方仓库、下载的工具和临时依赖,
因此平台不能假定沙箱内代码可信。Kuasar Sandbox 用 KVM MicroVM 作为计算隔离边界:
每个沙箱拥有独立 Guest Kernel,宿主只暴露受控的生命周期、存储、网络和执行接口。
在保留虚拟机安全边界的同时,周边系统面向短生命周期、高并发和有状态 Agent 工作负载组织。

## 核心能力

- **独立内核隔离**:每个沙箱运行在独立 Guest Kernel 中,由 KVM 和 MicroVM 提供
  工作负载边界.
- **快照模板实例化**:从一个预初始化模板创建多个身份独立的沙箱实例,共享只读
  模板父层,每个实例只维护自己的增量状态.
- **有状态暂停恢复**:暂停同一个逻辑沙箱并保留进程,内存和文件系统状态,随后在
  原节点或具备可移植工件的其他节点恢复稳定 Sandbox ID.
- **按需加载**:内存页和磁盘块在工作负载实际访问时加载,启动与恢复成本取决于工作集,
  而不是完整逻辑镜像大小.
- **灵活数据路径**:镜像,快照和稀疏工件可以使用本地文件,NAS/NFS 等共享文件
  存储,也可以使用 Manifest,S3-compatible 对象存储和分层缓存.
- **高密资源治理**:空闲时回收 CPU 和非活跃内存,长时间等待时暂停实例;节点以
  准入,动态预算,水位和安全余量保护并发活跃工作负载.
- **隔离网络基础**:快速分配和回收沙箱网络资源,以内核态数据路径转发,默认隔离
  沙箱,并向外部策略网关传递可信沙箱身份.
- **多租安全**:平台身份、限定作用域的数据面 capability、内容保护密钥、网络身份和节点
  资源预算保持独立的安全边界.
- **E2B 兼容入口**:`node-ctl conductor serve` 提供单节点 E2B 兼容服务,集群 router 提供
  多节点统一入口,均可由未修改的 E2B SDK 使用.
- **单节点与集群部署**:组件既可以组成单机平台,也可以通过 registry,router 和
  placer 组成 group-scoped 多节点控制面.

## 快照生命周期

同一套快照基础设施支持两种用户工作流。

### 从快照模板创建实例(1:N)

模板可包含已初始化的操作系统、语言运行时、依赖、工具与预热服务。多个新沙箱共享
不可变父状态,同时保有各自独立的身份和可写变化。

### 暂停与恢复一个逻辑实例(1:1)

暂停运行中的沙箱会保留进程、内存和文件系统状态;重新连接同一稳定身份会恢复该逻辑实例。
这支持长时间运行的 Agent、人工审批、空闲会话、节点维护和迁移。

两者都使用明确的父/子快照关系。存储效率首先来自共享已知父层、只记录实例变化,
不依赖独立运行的虚拟机偶然生成高度相同的内存快照。

## 数据与存储模型

并非所有工件都必须使用同一种后端:

| 数据路径 | 典型用途 | 特征 |
|---|---|---|
| 本地文件 | 单节点、本地 NVMe、节点亲和工作负载 | 路径短、外部依赖少 |
| 共享文件系统 | NAS、NFS 或在多节点一致挂载的文件系统 | 原生文件语义、直接跨节点访问 |
| Manifest 与对象存储 | 大规模分发、远程持久化、跨节点恢复和分层缓存 | 内容寻址组织、按需读取 |

`accelerator` 提供这些路径共用的稀疏数据表达、本地工件、完整性与加密、引用、Manifest
组织、文件系统与 S3-compatible Store、缓存、OCI 获取、EROFS 展平、fetch 与 prefetch。
内容寻址和去重在适合的数据/安全域内使用,不是唯一存储模型,也不假定每份独立 VM 快照都能有效去重。

## 高密资源治理

Agent 沙箱负载通常非线性:大部分时间等待模型、工具、外部 I/O 或人工输入,随后短时突发。
密度是安全利用资源的结果:

1. 空闲沙箱释放 CPU 和非活跃内存;
2. 长时间空闲的沙箱可暂停为持久状态;
3. 回收容量归还节点共享池;
4. 节点准入、动态 grant、水位、主动回收和安全余量保护并发突发;
5. 在声明的资源模型内,平台共享不能把节点压力转化为沙箱 OOM 或丢失有效工作。

`sandboxer` 执行单沙箱 cgroup、balloon 与 VMM 生命周期动作;`node-ctl` reservation
controller 拥有节点级准入、共享池记账、grant、inventory 对账和恢复。高密是在不把 OOM
当调度机制的前提下提高有效利用率,不是单纯追求 VM 数量。

## 网络与策略集成

`connector` 为 MicroVM 提供高密 eBPF/TC 数据路径,快速创建/释放沙箱网络资源,
默认不转发沙箱间流量,验证平台分配的网络身份,已有流量逐包转发无需用户态进程参与。

部署可把可信沙箱身份传给集中式外部策略网关,在那里实施逐沙箱公网、私网、DNS、代理、
审计与流量治理策略。节点本地轻量 Egress 仍是提议中的扩展,不是基础 vSwitch 的已交付能力。

## 架构

```text
Agent / E2B SDK / Platform API
                │
        orchestrator
                │
        sandboxer runtime
        ├── accelerator
        ├── connector
        └── guest-runtime
                │
KVM / Local File / NAS / Object Storage / Network
```

`kuasar-sandbox/kuasar-sandbox` 是 canonical project repository,负责系统级设计,
跨组件验证,公共 CI 环境和聚合发布.五个组件仓独立维护实现,组件 E2E 和版本线:

| 组件仓 | 主要职责 |
|---|---|
| [`orchestrator`](https://github.com/kuasar-sandbox/orchestrator) | E2B 兼容节点服务、节点资源准入、Proxy 集成与 Registry/Router/Placer 集群控制面 |
| [`sandboxer`](https://github.com/kuasar-sandbox/sandboxer) | MicroVM 生命周期、快照/恢复、Guest 控制、块设备和单沙箱资源执行 |
| [`accelerator`](https://github.com/kuasar-sandbox/accelerator) | 数据访问、存储、加密、内容组织、缓存、OCI 获取与镜像展平 |
| [`connector`](https://github.com/kuasar-sandbox/connector) | 高密 eBPF 网络、隔离、TAP 交接、可信沙箱网络身份与策略网关接入基础 |
| [`guest-runtime`](https://github.com/kuasar-sandbox/guest-runtime) | Guest runtime 镜像、Guest kernel、native Guest 依赖和镜像构建工具 |

五个组件可以组合成完整平台,也可以按场景独立采用,部署和发布.`guest-runtime`
提供 `runtime` 和 `vmlinux` 两个发布单元,但仍然是一个组件仓.

## 发行状态

- **Stable 通道**:GitHub Latest 指向的非 prerelease 聚合版本;一次安装先解析一次,再对全部资产和校验和固定同一精确 Tag.
- **Preview 通道**:作为 GitHub prerelease 保留,用于开发和评估,不替代当前 Stable.
- **预构建 Release 架构**:当前 GitHub Release 提供 Linux x86_64 资产.
- **源码构建架构**:当前 Makefile 支持 `TARGET_ARCH=x86_64` 和
  `TARGET_ARCH=aarch64`;源码可构建不表示该架构已经作为预构建 Release 资产发布.
- **版本关系**:组件独立发布版本,聚合版本固定选择一组精确组件版本并在真实 KVM
  上完成跨组件验证.

`Stable` 表示非 prerelease 的公开聚合版本,`Preview` 表示用于开发和评估的
prerelease 聚合版本,`Proposed` 表示仍在 Issue 或设计阶段且不能作为已交付能力
使用.当前主要支持范围为:

| 范围 | 当前状态 |
|---|---|
| 单节点 | Available;`node-ctl conductor serve` 提供 E2B 兼容入口 |
| 多节点集群 | Available;需要 registry,router,placer 和集群基础设施 |
| 本地文件,共享文件,对象存储 | Available;部署方按节点亲和,共享访问和远程持久化需求选择 |
| 基础 vSwitch 和沙箱隔离 | Available |
| 外部集中式策略网关 | Integration supported;已有可信沙箱身份传递和接入基础 |
| 节点本地轻量 Egress | [Proposed](https://github.com/kuasar-sandbox/connector/issues/9) |
| OpenTelemetry | [Proposed](https://github.com/kuasar-sandbox/kuasar-sandbox/issues/52) |

最新可用资产和 prerelease 状态以
[GitHub Releases](https://github.com/kuasar-sandbox/kuasar-sandbox/releases) 为准.

## 快速开始

[快速开始](docs/quickstart_zh.md) 从同一聚合版本下载全部显式资产、校验 `SHA256SUMS`,
准备单节点,并用上游 E2B Python SDK 构建就绪的快照模板。Demo 从精确 Build 的 ready
状态读取已发布 Template ID;SDK 返回的注册句柄不是这个 ID。保持节点运行并完成文档中的
SDK 连接配置后,把 `TEMPLATE_ID` 设置为该已发布 ID,再执行以下生命周期:

```python
import os
from e2b import Sandbox

sandbox = Sandbox.create(os.environ["TEMPLATE_ID"], timeout=300)
sandbox_id = sandbox.sandbox_id
print(sandbox.commands.run("uname -sm").stdout)
sandbox.pause()
sandbox = Sandbox.connect(sandbox_id)  # reconnect and auto-resume
print(sandbox.commands.run("echo resumed").stdout)
sandbox.kill()
```

完整本地环境、模板实例扇出、网络访问和迁移流程见 [Demo](test/demo/DEMO_zh.md)。

## Development

标准本地工作区由六个兄弟仓组成:

```text
<workspace>/
├── kuasar-sandbox/
├── accelerator/
├── connector/
├── sandboxer/
├── guest-runtime/
└── orchestrator/
```

主仓主要目录:

```text
docs/             System design, deployment, performance, CI, and release docs
test/e2e/         Owner suites, run_all.sh, and platform integration cases
test/perf|demo/   Platform performance and demo scripts
ci/integration/   CI helpers and source-cache maintenance
ci/native-cache/  vmlinux, erofs, envd, RocksDB, and Cloud Hypervisor cache
ci/runner/        Self-hosted CI runner deployment
release/          Version resolution, packaging, aggregation, and publishing
releases/         Stable and daily Preview aggregate selections
.github/workflows/Reusable CI, aggregate release, and daily Preview workflows
```

## Build and Test

完整源码构建从主仓驱动五个组件仓,并按
[`release/bin-inputs.manifest`](release/bin-inputs.manifest) 把运行文件收集到
`bin/<arch>/`:

```bash
make -C kuasar-sandbox build
make -C kuasar-sandbox test
make -C kuasar-sandbox test-e2e
make -C kuasar-sandbox perf
make -C kuasar-sandbox demo
```

组件用例位于各自仓的 `test/e2e/`,入口统一为 `run_all.sh`.`make test-e2e` 先把候选
组件源码与其余组件源码组装为 `test/e2e/<owner>/` 布局,再运行与 platform 发布包完全相同
的顶层入口;需要多仓制品的用例仍由其功能所属组件维护.

单仓构建仍从组件仓执行.例如:

```bash
GOWORK=off make -C sandboxer build
```

本地发布工具测试不访问 GitHub,也不构建真实 native 依赖:

```bash
make -C kuasar-sandbox test-release-tools
make -C kuasar-sandbox test-ci-tools
make -C kuasar-sandbox test-perf-tools
```

需要 KVM、root、eBPF、systemd、外部存储或完整兄弟源码集的测试分别声明这些前置条件。
跳过特权用例不能解释为完成集成验收。

## Release

组件独立发布以下版本线:

- `accelerator`、`connector`、`sandboxer`、`orchestrator`: `vX.Y.Z`;
- Guest runtime: `runtime-vX.Y.Z`;
- Guest kernel: `vmlinux-vX.Y.Z`.

主仓发布 `release-vX.Y.Z` 聚合版本,各组件可以选择不同版本号。
`releases/release.yaml` 选择下一 Stable,`releases/daily-preview.yaml` 选择当前 Daily Preview。
聚合工作流解析精确组件 Tag、校验声明的资产与校验和、组装 platform archive,
并在发布前运行跨组件验证。

当前 x86_64 聚合版本包含一个 platform archive、六个发布单元 archive 和统一
`SHA256SUMS`。源码仓保持独立,聚合版本是经过测试的组合契约。

完整的两清单/历史和基线规则、维护分支选择、资产排除规则、人工发布命令、权限边界与
失败恢复均由 [Release 规范](docs/release_zh.md) 维护。

## Documentation

- [Architecture](docs/kuasar-sandbox_zh.md):系统目标,架构和关键语义;
- [Deployment](docs/deployment_zh.md):部署拓扑,进程,端口与启停依赖;
- [Performance](docs/perf_zh.md):带测试上下文的性能基线,回归门禁与调优方法;
- [CI](docs/ci_zh.md):端到端集成测试,缓存,候选 revision 和发行资产验证;
- [Releases](docs/release_zh.md):组件版本,聚合选择,资产与发布事务;
- [快速开始](docs/quickstart_zh.md):从聚合 Release 到真实 MicroVM 的首次使用路径;
- [Demo](test/demo/DEMO_zh.md):本地体验环境与 SDK 演示;
- [Full validation](test/QUICKSTART_zh.md):发布包解压,环境准备与完整 E2E 入口.
- [安全策略](SECURITY_zh.md):支持范围和私密漏洞报告方式.

维护中的设计与参考文档提供完整中英文版本。英文使用默认文件名,中文使用 `_zh.md`,
并带双向语言选择器。遵循 [文档贡献政策](CONTRIBUTING_zh.md#文档贡献)。

## 贡献与安全

修改前阅读组织级 [贡献指南(英文)](https://github.com/kuasar-sandbox/.github/blob/main/CONTRIBUTING.md)。
选择拥有该行为的仓库、保持 PR 聚焦,跨仓修改须双向关联配套 PR。
不要在公开 Issue 或讨论中披露安全漏洞;遵循 [安全策略](SECURITY_zh.md),使用 GitHub 私密漏洞报告。

## License

本仓库的项目原创内容采用 [Apache License 2.0](LICENSE).
第三方及其他许可的内容保留自己的声明与义务。贡献授权说明见 [CONTRIBUTING_zh.md](CONTRIBUTING_zh.md).
