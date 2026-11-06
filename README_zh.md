[English](README.md) | [简体中文](README_zh.md)

# Kuasar Sandbox

Kuasar Sandbox 是面向 AI Agent、Serverless、代码执行和强化学习环境的 MicroVM 沙箱平台。
每个沙箱拥有独立 Guest 内核，平台结合可复用模板、有状态暂停恢复、按需数据加载和节点资源治理。

## 快速导航

| 目标 | 指南 |
| --- | --- |
| 运行第一个真实沙箱 | [快速开始](docs/quickstart_zh.md) |
| 取得匹配的版本组合 | [安装](docs/installation_zh.md) |
| 构建应用模板 | [模板](docs/templates_zh.md) |
| 执行工作并管理保留状态 | [沙箱使用](docs/sandboxes_zh.md) |
| 理解能力和对象 | [概念](docs/concepts_zh.md)、[架构与功能树](docs/architecture_zh.md) |
| 部署和运行平台 | [部署](docs/deployment_zh.md)、[运维](docs/operations_zh.md)、[安全边界](docs/security_zh.md) |
| 更换版本或评估容量 | [升级](docs/upgrading_zh.md)、[性能](docs/performance_zh.md) |

## 能力与组件职责

平台可以从只读模板父层创建独立实例，也可以暂停后恢复同一个逻辑实例。
内存快照保存进程状态，仅磁盘保存状态会重新启动进程。明确的父层关系支持共享，
同时各实例保留自己的身份和可写变化。可移植状态仍要求目标端具备兼容执行输入、
完整依赖、密钥、网络所有权和资源准入条件。

数据可采用本地文件、命名共享文件系统和 Manifest/对象存储路径。Cache 加速访问，
不能替代持久制品保留。宿主建立的网络身份与不同作用域的凭据隔离平台使用者和 Guest 工作负载，
外部网关负责所选策略。

| 组件 | 职责 |
| --- | --- |
| [orchestrator](https://github.com/kuasar-sandbox/orchestrator) | 受管生命周期、Build、访问、节点准入、观测、扩展及 Registry/Router/Placer 控制 |
| [sandboxer](https://github.com/kuasar-sandbox/sandboxer) | Runtime、E/S 制品、内存、块存储、本地资源执行与 Guest/VMM 集成 |
| [accelerator](https://github.com/kuasar-sandbox/accelerator) | 通用数据契约、传输处理、持久 Store、Cache 和镜像生产库 |
| [connector](https://github.com/kuasar-sandbox/connector) | eBPF/TC 网络、隔离、可信身份、网关集成及 TAPFD 交接 |
| [guest-runtime](https://github.com/kuasar-sandbox/guest-runtime) | Guest Runtime bundle、独立版本化的 VMLinux、flatten CLI 和原生交付输入 |

主仓维护跨组件设计、验证、开发基础设施和聚合发布，各组件仍可独立使用。
[功能树](docs/architecture_zh.md#功能树与权威主题)列明各能力的权威文档与实现边界。

## 第一个应用

按[快速开始](docs/quickstart_zh.md)取得并校验一个聚合版本，在匹配 Workbench 中运行真实 Demo。
Workbench 提供私有开发和测试环境，原生生产部署独立运行。Demo 完成后会停止临时节点；
继续使用时，应保留明确运行的服务和连接设置，再按完整的[模板](docs/templates_zh.md)与
[实例](docs/sandboxes_zh.md)示例操作。

成功要求真实完成模板、创建、命令、文件、暂停恢复和删除流程。下载完成、进程启动或跳过
特权测试均不能证明该结果。[完整验证指南](test/QUICKSTART_zh.md)覆盖 Demo 之外的聚合 E2E。

## 版本与支持范围

选择一个 [Stable 版本](https://github.com/kuasar-sandbox/kuasar-sandbox/releases/latest)或明确的 Preview，
保留精确 Tag。聚合版本选择六个组件发布单元：accelerator、connector、sandboxer、orchestrator、
Runtime 和 VMLinux，它们的版本可以不同。新聚合版本提供原生 Linux x86_64 和 aarch64 输入，
历史版本保留其已经发布的架构集合。

Stable/Preview 表示发行通道。生产部署仍需验收容量，并配置 TLS、可靠存储、网络策略与凭据。
源码能力、提案和测量结果在[架构](docs/architecture_zh.md)与[性能](docs/performance_zh.md)中分别说明。
[发布维护](docs/release_zh.md)说明版本选择与发布，[升级](docs/upgrading_zh.md)说明对保留工作负载的影响。

## 文档

[文档目录](docs/README_zh.md)提供全部平台任务和组件契约。英文使用默认文件名，持续维护的
中文对应文件采用 `_zh.md`。已有英文单语文档仍符合要求。

开发工作从[源码工作区与构建测试](CONTRIBUTING_zh.md#source-workspace)、[Workbench](workbench/README_zh.md)、
[测试组织](test/README_zh.md)和 [CI](docs/ci_zh.md)开始。性能方法与带版本证据见[性能](docs/performance_zh.md)。

## 贡献与安全

阅读[贡献指南](CONTRIBUTING_zh.md)，选择拥有该行为的仓库。跨仓变更使用明确 companion 引用协调。
安全漏洞通过 [SECURITY](SECURITY_zh.md) 中的私有渠道报告。

## 许可证

项目原创内容采用 [Apache License 2.0](LICENSE)。第三方和其他许可证材料保留各自声明与署名，
[贡献指南](CONTRIBUTING_zh.md)说明贡献许可。
