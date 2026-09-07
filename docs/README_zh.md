[English](README.md) | [简体中文](README_zh.md)

# 文档

## 用户与系统文档

- [快速开始](quickstart_zh.md)
- [系统总览](kuasar-sandbox.md)
- [部署](deployment.md)
- [性能方法](perf.md)
- [发布契约](release.md)
- [CI 与 BMS](ci.md)
- [完整发布验证](../test/QUICKSTART_zh.md)
- [演示](../test/demo/DEMO_zh.md)
- [Runner 运维（英文）](../ci/runner/README.md)

详细设计和运维文档的全量英文化由 [#86](https://github.com/kuasar-sandbox/kuasar-sandbox/issues/86) 跟踪。[逐文件清单（英文）](documentation-inventory.md)及关联实现任务区分完成和待翻译内容。导航标题为英文不代表目标已经完成翻译；迁移中逐项调整中文对应路径。

## 组件规范

各个独立演进的组件拥有详细文档：

- [orchestrator](https://github.com/kuasar-sandbox/orchestrator/tree/main/docs)：节点和集群控制面、路由、资源。
- [sandboxer](https://github.com/kuasar-sandbox/sandboxer/tree/main/docs)：生命周期、工件、Guest ABI、VMM 补丁。
- [accelerator](https://github.com/kuasar-sandbox/accelerator/tree/main/docs)：数据访问、Manifest、Store、Cache。
- [connector](https://github.com/kuasar-sandbox/connector/tree/main/docs)：vSwitch 与 TAP FD 协议。
- [guest-runtime](https://github.com/kuasar-sandbox/guest-runtime/tree/main/docs)：Runtime Bundle、内核和镜像展平。

仓库访问取决于协调公开的当前状态；私有链接不能作为匿名验收已完成的证据。

## 维护文档

- [语言与检视规范](documentation-policy_zh.md)
- [术语](terminology_zh.md)
- [迁移清单（英文）](documentation-inventory.md)
- [文档打包](documentation-packaging_zh.md)
- [贡献规范（英文）](../CONTRIBUTING.md)
- [安全报告](../SECURITY_zh.md)
