# Kuasar Sandbox

Kuasar Sandbox 是一套支持生产部署的 MicroVM 沙箱平台,面向大规模 Agent,
Serverless 与强化学习工作负载,提供独立 Guest Kernel 隔离,快照模板实例化,
有状态暂停恢复,按需数据加载,高密资源治理,以及从单节点到多节点集群的完整能力.

系统支持生产部署.当前 Stable 聚合版本为
[`release-v0.1.1`](https://github.com/kuasar-sandbox/kuasar-sandbox/releases/tag/release-v0.1.1);
Preview 继续用于开发和评估.生产就绪描述系统的部署能力,公开发行通道标记描述资产
和接口的稳定性,两者是不同维度.生产部署仍应结合工作负载完成容量验证,
并配置正式 TLS,可靠存储,网络策略和安全凭据.

## 快速导航

- [Quick Start](docs/quickstart.md):从同一聚合 Release 下载资产,校验并用 E2B SDK 运行首个真实 MicroVM;
- [Architecture](docs/kuasar-sandbox.md):系统能力,组件边界和关键语义;
- [Deployment](docs/deployment.md):单节点,集群拓扑与进程依赖;
- [Releases](docs/release.md):组件版本,聚合版本和资产契约;
- [Demo](test/demo/DEMO.md):本地体验环境和 E2B SDK 演示;
- [Full validation](test/QUICKSTART.md):完整 Aggregate Release E2E 验收;
- [Security](SECURITY.md):支持范围和私密漏洞报告入口.

## 核心能力

- **独立内核隔离**:每个沙箱运行在独立 Guest Kernel 中,由 KVM 和 MicroVM 提供
  工作负载边界.
- **快照模板实例化**:从一个预初始化模板创建多个身份独立的沙箱实例,共享只读
  模板父层,每个实例只维护自己的增量状态.
- **有状态暂停恢复**:暂停同一个逻辑沙箱并保留进程,内存和文件系统状态,随后在
  原节点或具备可移植工件的其他节点恢复稳定 Sandbox ID.
- **灵活数据路径**:镜像,快照和稀疏工件可以使用本地文件,NAS/NFS 等共享文件
  存储,也可以使用 Manifest,S3-compatible 对象存储和分层缓存.
- **高密资源治理**:空闲时回收 CPU 和非活跃内存,长时间等待时暂停实例;节点以
  准入,动态预算,水位和安全余量保护并发活跃工作负载.
- **隔离网络基础**:快速分配和回收沙箱网络资源,以内核态数据路径转发,默认隔离
  沙箱,并向外部策略网关传递可信沙箱身份.
- **E2B 兼容入口**:`node-ctl conductor serve` 提供单节点 E2B 兼容服务,集群 router 提供
  多节点统一入口,均可由未修改的 E2B SDK 使用.
- **单节点与集群部署**:组件既可以组成单机平台,也可以通过 registry,router 和
  placer 组成 group-scoped 多节点控制面.

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
跨组件验证,公共 BMS 环境和聚合发布.五个组件仓独立维护实现,组件 E2E 和版本线:

| 组件仓 | 主要职责 |
|---|---|
| [`orchestrator`](https://github.com/kuasar-sandbox/orchestrator) | 单节点和集群控制面,E2B 兼容入口,节点资源准入与恢复 |
| [`sandboxer`](https://github.com/kuasar-sandbox/sandboxer) | MicroVM 生命周期,快照/恢复,Guest 协同和单沙箱资源执行 |
| [`accelerator`](https://github.com/kuasar-sandbox/accelerator) | 镜像,快照和稀疏工件的数据访问,存储,缓存与 OCI 展平 |
| [`connector`](https://github.com/kuasar-sandbox/connector) | eBPF/TC vSwitch,TAP 交接,隔离网络和外部网关接入基础 |
| [`guest-runtime`](https://github.com/kuasar-sandbox/guest-runtime) | Guest runtime 镜像与 Guest kernel 构建和发布 |

五个组件可以组合成完整平台,也可以按场景独立采用,部署和发布.`guest-runtime`
提供 `runtime` 和 `vmlinux` 两个发布单元,但仍然是一个组件仓.

## 发行状态

- **Current stable release**:[`release-v0.1.1`](https://github.com/kuasar-sandbox/kuasar-sandbox/releases/tag/release-v0.1.1).
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
ci/bms/           BMS helpers and source-cache maintenance
ci/native-cache/  vmlinux, erofs, envd, RocksDB, and Cloud Hypervisor cache
ci/runner/        Self-hosted BMS runner deployment
release/          Version resolution, packaging, aggregation, and publishing
releases/         Stable and daily Preview aggregate selections
.github/workflows/Reusable BMS, aggregate release, and daily Preview workflows
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

## Release

组件独立发布以下版本线:

- `accelerator`、`connector`、`sandboxer`、`orchestrator`: `vX.Y.Z`;
- Guest runtime: `runtime-vX.Y.Z`;
- Guest kernel: `vmlinux-vX.Y.Z`.

主仓只发布聚合版本 `release-vX.Y.Z`.聚合版本可以选择不同的组件版本.每个受维护平台
分支以 `releases/release.yaml` 描述下一 Stable,以 `releases/daily-preview.yaml` 描述
当前 Daily Preview,并强制 `daily version >= release version`.代码库只维护这两个当前
清单;历史选择由同一文件的 first-parent Git 提交历史保存,不建立第二套 history 目录或
版本文件.
正式版只引用上一正式版;Preview 优先引用同一正式版本线的上一 Preview,该版本线的
首个 Preview 则引用上一正式版.第一个正式版本不设置基线,也不把 Preview 或全部提交
历史作为发布说明 diff.

Daily scanner 自动处理平台 `main` 和全部 `release/vMAJOR.MINOR.x`.主线只扫描各组件
`main`;平台维护分支按每个 unit 在 Daily 清单中的独立版本映射组件
`release/vMAJOR.MINOR.x`,分支不存在时固定复用该 unit 的完整 Release.Tag 选择按
first-parent 上最近的带 Tag 提交,仅同一提交上的多个 Tag 比较 SemVer.残缺 Daily-owned
Preview 会连同资产和 Tag 一起清理后重扫;外来残缺版本只忽略.

组件 Release 的显式资产只有目标 archive 与 `SHA256SUMS`.当前 x86_64 聚合 Release
精确包含 platform 包,六个发布单元 archive 和统一 `SHA256SUMS`,不发布项目生成的
release metadata JSON,也不重复上传 GitHub 已自动提供的源码归档.版本选择 YAML 只在
代码库维护,既不是 Release 资产,也不进入 platform 包.组件 archive 不携带 `docs/` 或
`test/e2e/`;aggregate prepare 从所选组件 tag 的 GitHub 源码归档收集它们,统一写入
platform 包.

人工收敛一个已经配置的聚合版本:

```bash
RELEASE_VERSION=$(awk '$1 == "version:" {print $2}' \
  kuasar-sandbox/releases/release.yaml)
PLATFORM_REF=$(git -C kuasar-sandbox branch --show-current)
PLATFORM_SHA=$(git -C kuasar-sandbox rev-parse HEAD)
make -C kuasar-sandbox release RELEASE_VERSION="$RELEASE_VERSION" \
  PLATFORM_REF="$PLATFORM_REF" PLATFORM_SHA="$PLATFORM_SHA"
```

也可以在组件版本已发布后只触发聚合验证与发布:

```bash
gh workflow run aggregate-release.yml \
  --repo kuasar-sandbox/kuasar-sandbox --ref main \
  -f version="$RELEASE_VERSION" \
  -f source_ref="$PLATFORM_REF" -f source_sha="$PLATFORM_SHA"
```

详细资产契约,权限边界,失败恢复和每日 Preview 状态机见
[docs/release.md](docs/release.md).

## Documentation

- [Architecture](docs/kuasar-sandbox.md):系统目标,架构和关键语义;
- [Deployment](docs/deployment.md):部署拓扑,进程,端口与启停依赖;
- [Performance](docs/perf.md):带测试上下文的性能基线,回归门禁与调优方法;
- [CI](docs/ci.md):可复用 BMS,缓存,候选 revision 和 exact-asset 模式;
- [Releases](docs/release.md):组件版本,聚合选择,资产与发布事务;
- [Quick Start](docs/quickstart.md):从聚合 Release 到真实 MicroVM 的首次使用路径;
- [Demo](test/demo/DEMO.md):本地体验环境与 SDK 演示;
- [Full validation](test/QUICKSTART.md):发布包解压,环境准备与完整 E2E 入口.
- [Security](SECURITY.md):支持范围和私密漏洞报告方式.

## License

本仓库的项目原创内容采用 [Apache License 2.0](LICENSE).
贡献授权说明见 [CONTRIBUTING.md](CONTRIBUTING.md).
