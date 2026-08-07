# Kuasar Sandbox Platform

`platform` 是 Kuasar Sandbox 的系统集成与发行仓。它与 `accelerator`、`connector`、
`sandboxer`、`guest-runtime`、`orchestrator` 并列,承载系统级设计文档、跨仓 E2E/perf/demo、
BMS 公共实现、原生制品缓存和 `release-vX.Y.Z` 聚合发布。组件实现及组件独立版本仍归各自仓库。

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
test/             跨组件 E2E、性能和演示脚本
ci/bms/           BMS 辅助工具与源码缓存维护
ci/native-cache/  vmlinux、erofs、envd、RocksDB、Cloud Hypervisor 缓存
ci/runner/        自托管 BMS runner 部署资料
release/          版本解析、打包、聚合、发布和每日协调器
releases/         正式聚合版本选择与每日 preview 基线
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
`releases/release-vX.Y.Z.yaml`;每日 preview 由 `releases/daily-preview.yaml` 的正式基线和
上海日期确定性派生,不为每天的版本修改 `main`。

组件 Release 的显式资产只有目标 archive 与 `SHA256SUMS`。当前 x86_64 聚合 Release
精确包含 platform 包、六个组件 archive 和统一 `SHA256SUMS`,不发布项目生成的 release
metadata JSON,也不重复上传 GitHub 已自动提供的源码归档。

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
