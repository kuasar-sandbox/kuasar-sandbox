# Kuasar Sandbox Platform

`platform` 是 Kuasar Sandbox 的系统集成与发行仓。它与 `accelerator`、`connector`、
`sandboxer`、`guest-runtime`、`orchestrator` 并列,承载系统级设计文档、E2E 组装与统一环境、
跨组件组合用例、perf/demo、BMS 公共实现、原生制品缓存和 `release-vX.Y.Z` 聚合发布。组件
实现、组件 E2E 与组件独立版本仍归各自仓库。

平台面向大规模 Serverless 与 Agent 场景,组合 microVM 生命周期、内容寻址存储与快照恢复、
eBPF/TC 网络、Guest runtime 及单节点/集群编排能力。整体架构见
[docs/kuasar-sandbox.md](docs/kuasar-sandbox.md),部署与验证入口分别见
[docs/deployment.md](docs/deployment.md) 和 [test/QUICKSTART.md](test/QUICKSTART.md)。

## 工程布局

标准本地工作区由六个兄弟仓组成:

```text
<workspace>/
├── platform/
├── accelerator/
├── connector/
├── sandboxer/
├── guest-runtime/
└── orchestrator/
```

本仓主要目录:

```text
docs/             系统总览、部署、性能、CI 与发布设计
test/e2e/         owner 套件组装器、统一 run_all.sh 与 platform 组合用例
test/perf|demo/   平台性能和演示脚本
ci/bms/           BMS 辅助工具与源码缓存维护
ci/native-cache/  vmlinux、erofs、envd、RocksDB、Cloud Hypervisor 缓存
ci/runner/        自托管 BMS runner 部署资料
release/          版本解析、打包、聚合、发布和每日协调器
releases/         当前正式版与每日 preview 的两个聚合选择清单
.github/workflows/唯一的可复用 BMS、聚合发布和每日 preview 工作流
```

## 构建与测试

完整源码构建从本仓驱动五个组件仓,并按
[`release/bin-inputs.manifest`](release/bin-inputs.manifest) 把运行文件收集到
`bin/<arch>/`:

```bash
make -C platform build
make -C platform test
make -C platform test-e2e
make -C platform perf
make -C platform demo
```

组件用例位于各自仓的 `test/e2e/`,入口统一为 `run_all.sh`。`make test-e2e` 先把候选
组件源码与其余组件源码组装为 `test/e2e/<owner>/` 布局,再运行与 platform 发布包完全相同
的顶层入口;需要多仓制品的用例仍由其功能所属组件维护。

单仓构建仍从组件仓执行。例如:

```bash
GOWORK=off make -C sandboxer build
```

本地发布工具测试不访问 GitHub,也不构建真实 native 依赖:

```bash
make -C platform test-release-tools
make -C platform test-ci-tools
make -C platform test-perf-tools
```

## 版本与发布

组件独立发布以下版本线:

- `accelerator`、`connector`、`sandboxer`、`orchestrator`: `vX.Y.Z`;
- Guest runtime: `runtime-vX.Y.Z`;
- Guest kernel: `vmlinux-vX.Y.Z`。

本仓只发布聚合版本 `release-vX.Y.Z`。聚合版本可选择不同的组件版本,正式版本选择写入
`releases/release.yaml`;每日协调器根据组件仓是否存在新提交更新
`releases/daily-preview.yaml`,再按冻结后的选择收敛组件与聚合发布。代码库只维护这两个
当前清单;历史选择由同一文件的 Git 提交历史保存,不建立第二套 history 目录或版本文件。
正式版只引用上一正式版;preview 优先引用同一正式版本线的上一 preview,该版本线的首个
preview 则引用上一正式版。第一个正式版本不设置基线,也不把 preview 或全部提交历史作为
发布说明 diff。

组件 Release 的显式资产只有目标 archive 与 `SHA256SUMS`。当前 x86_64 聚合 Release
精确包含 platform 包、六个组件 archive 和统一 `SHA256SUMS`,不发布项目生成的 release
metadata JSON,也不重复上传 GitHub 已自动提供的源码归档。版本选择 YAML 只在代码库维护,
既不是 Release 资产,也不进入 platform 包。组件 archive 不携带 `docs/` 或 `test/e2e/`;
aggregate prepare 从所选组件 tag 的 GitHub 源码归档收集它们,统一写入 platform 包。

人工收敛一个已经配置的聚合版本:

```bash
make -C platform release RELEASE_VERSION=release-v0.1.0
```

也可以在组件版本已发布后只触发聚合验证与发布:

```bash
gh workflow run aggregate-release.yml \
  --repo kuasar-sandbox/platform --ref main \
  -f version=release-v0.1.0
```

详细资产契约、权限边界、失败恢复和每日 preview 状态机见
[docs/release.md](docs/release.md)。

## 文档

- [docs/kuasar-sandbox.md](docs/kuasar-sandbox.md):系统目标、架构和关键机制;
- [docs/deployment.md](docs/deployment.md):部署拓扑、进程、端口与启停依赖;
- [docs/perf.md](docs/perf.md):性能基线、回归门禁与调优方法;
- [docs/ci.md](docs/ci.md):可复用 BMS、缓存、候选 revision 和 exact-asset 模式;
- [docs/release.md](docs/release.md):组件版本、聚合选择、资产与发布事务;
- [test/QUICKSTART.md](test/QUICKSTART.md):发布包解压、环境准备与 E2E 入口。
