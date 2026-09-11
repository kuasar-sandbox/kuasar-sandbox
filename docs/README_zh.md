[English](README.md) | [简体中文](README_zh.md)

# 文档

## 开始使用与部署运维

- [快速开始](quickstart_zh.md)：使用同一聚合版本运行第一个沙箱。
- [部署](deployment_zh.md)：选择拓扑、服务、存储与运维依赖。
- [完整 E2B 演示](../test/demo/DEMO_zh.md)：生命周期、网络、模板扇出与迁移。
- [聚合发布验证](../test/QUICKSTART_zh.md)：验收交付资产，不是首次体验的源码构建指南。

## 系统与组件契约

- [系统总览](kuasar-sandbox_zh.md)：用户语义与五个组件的职责边界。
- [术语](terminology_zh.md)：跨组件词汇速查；精确字段语义由所属组件定义。
- [orchestrator](https://github.com/kuasar-sandbox/orchestrator/tree/main/docs)：节点生命周期、Build、资源控制、Registry、Router、Placer 与扩展。
- [sandboxer](https://github.com/kuasar-sandbox/sandboxer/tree/main/docs)：运行生命周期、便携工件、Guest ABI 与 VMM 集成。
- [accelerator](https://github.com/kuasar-sandbox/accelerator/tree/main/docs)：Manifest、文件工件、Store 与 Cache。
- [connector](https://github.com/kuasar-sandbox/connector/tree/main/docs)：vSwitch 设计及运维、独立 TAP FD 协议。
- [guest-runtime](https://github.com/kuasar-sandbox/guest-runtime/tree/main/docs)：镜像展平、Runtime Bundle 与 Kernel。

## 开发、验证与发布

- [贡献与文档检视](../CONTRIBUTING_zh.md#文档贡献)。
- [性能方法](perf_zh.md)：计时边界、证据与回归入口。
- [持续集成](ci_zh.md)：精确源码集合、信任边界与验证模式。
- [发布契约](release_zh.md)：版本选择、资产、发布恢复与文档打包。
- [Runner 运维](../ci/runner/README_zh.md)：安装与运维验证 Runner。
- [安全报告](../SECURITY_zh.md)。

英文为默认入口；维护中的中文版本在各配对文档顶部互链。组件访问取决于仓库可见性；已认证的源码检视不代表匿名访问验收完成。
