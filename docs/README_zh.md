[English](README.md) | [简体中文](README_zh.md)

# 文档目录

按要完成的任务选择入口。平台指南保留完整操作流程，组件规范维护精确行为和实现边界。

## 平台指南

| 文档 | 阅读目标 |
| --- | --- |
| [概念与术语](concepts_zh.md) | 理解实例、模板、E/S、身份与父层 |
| [架构与功能树](architecture_zh.md) | 查看能力分层、路径和权威归属 |
| [快速开始](quickstart_zh.md) | 完成第一次真实运行与清理 |
| [安装](installation_zh.md) | 选择、下载、校验与安装匹配版本 |
| [沙箱使用](sandboxes_zh.md) | 实例操作、访问、暂停恢复和重置 |
| [模板](templates_zh.md) | 构建、验证、发布和复用应用 |
| [部署](deployment_zh.md) | 装配单节点或集群并验收 |
| [安全边界](security_zh.md) | 配置身份、隔离、内容保护和可信路径 |
| [运维](operations_zh.md) | 诊断、恢复、保留和清理 |
| [升级](upgrading_zh.md) | 验证兼容性、改变版本和回退 |
| [性能与容量](performance_zh.md) | 测量、验收容量和调优 |
| [CI](ci_zh.md) | 跨仓源码选择和验证流程 |
| [发布维护](release_zh.md) | 组件/聚合版本、打包和发布 |

## 组件契约

| 组件 | 主题 |
| --- | --- |
| orchestrator | [node](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node_zh.md), [sandbox-lifecycle](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/sandbox-lifecycle_zh.md), [node-build](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-build_zh.md), [node-proxy](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-proxy_zh.md), [node-resource](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-resource_zh.md), [observability](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/observability_zh.md), [cluster](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/cluster_zh.md), [extensions](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/extensions_zh.md) |
| sandboxer | [runtime](https://github.com/kuasar-sandbox/sandboxer/blob/main/docs/runtime_zh.md), [configuration](https://github.com/kuasar-sandbox/sandboxer/blob/main/docs/configuration_zh.md), [artifacts](https://github.com/kuasar-sandbox/sandboxer/blob/main/docs/artifacts_zh.md), [memory](https://github.com/kuasar-sandbox/sandboxer/blob/main/docs/memory_zh.md), [block-storage](https://github.com/kuasar-sandbox/sandboxer/blob/main/docs/block-storage_zh.md), [resources](https://github.com/kuasar-sandbox/sandboxer/blob/main/docs/resources_zh.md), [guest-abi](https://github.com/kuasar-sandbox/sandboxer/blob/main/docs/guest-abi_zh.md), [vmm](https://github.com/kuasar-sandbox/sandboxer/blob/main/docs/vmm_zh.md) |
| accelerator | [artifacts](https://github.com/kuasar-sandbox/accelerator/blob/main/docs/artifacts_zh.md), [transfer](https://github.com/kuasar-sandbox/accelerator/blob/main/docs/transfer_zh.md), [store](https://github.com/kuasar-sandbox/accelerator/blob/main/docs/store_zh.md), [cache](https://github.com/kuasar-sandbox/accelerator/blob/main/docs/cache_zh.md), [image-preparation](https://github.com/kuasar-sandbox/accelerator/blob/main/docs/image-preparation_zh.md) |
| connector | [vswitch-operations](https://github.com/kuasar-sandbox/connector/blob/main/docs/vswitch-operations_zh.md), [vswitch](https://github.com/kuasar-sandbox/connector/blob/main/docs/vswitch_zh.md), [tapfd](https://github.com/kuasar-sandbox/connector/blob/main/docs/tapfd_zh.md) |
| guest-runtime | [using-guest-runtime](https://github.com/kuasar-sandbox/guest-runtime/blob/main/docs/using-guest-runtime_zh.md), [runtime-bundle](https://github.com/kuasar-sandbox/guest-runtime/blob/main/docs/runtime-bundle_zh.md), [vmlinux](https://github.com/kuasar-sandbox/guest-runtime/blob/main/docs/vmlinux_zh.md), [flatten-cli](https://github.com/kuasar-sandbox/guest-runtime/blob/main/docs/flatten-cli_zh.md), [build](https://github.com/kuasar-sandbox/guest-runtime/blob/main/docs/build_zh.md) |

## 开发、验证与示例

- [贡献、源码工作区和构建测试](../CONTRIBUTING_zh.md)
- [Workbench 环境](../workbench/README_zh.md)
- [测试组织](../test/README_zh.md)
- [发布版验证](../test/QUICKSTART_zh.md)
- [完整 E2B Demo](../test/demo/DEMO_zh.md)
- [CI runner 运维](../ci/runner/README_zh.md)
- [漏洞报告政策](../SECURITY_zh.md)

运行时与用户文档随所选版本打包；未打包的开发文档链接到选定源码版本。以当前源码指南解释历史发布时，必须先核对该版本实际能力。
