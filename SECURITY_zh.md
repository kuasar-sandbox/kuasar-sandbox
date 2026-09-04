[English](SECURITY.md) | [简体中文](SECURITY_zh.md)

# Security Policy

Kuasar Sandbox 通过项目主仓统一接收系统级和跨组件安全报告，并在私密流程中路由给 `orchestrator`、`sandboxer`、`accelerator`、`connector` 或 `guest-runtime` 维护者。

## 当前支持范围

当前支持的公开 Stable 聚合版本是 [`release-v0.1.2`](https://github.com/kuasar-sandbox/kuasar-sandbox/releases/tag/release-v0.1.2)。请报告影响该版本、最新已发布 Aggregate Preview 或当前 `main` 的漏洞，并提供实际受影响的精确聚合版本和组件版本。较早 Preview 中的问题会根据是否仍影响当前 Stable、最新 Preview 或 `main` 进行评估。

| 版本范围 | 当前状态 |
| --- | --- |
| `release-v0.1.2` | 当前 Stable 支持版本 |
| 最新已发布 Aggregate Preview | 接受预发布通道问题报告 |
| 当前 `main` | 接受源码问题报告 |

Preview 是 GitHub prerelease；系统是否支持生产部署与公开发行通道的稳定性标记是两个不同维度。

## 私密报告漏洞

请使用本仓库 Security 页面的 **Report a vulnerability** 提交 [private vulnerability report](https://github.com/kuasar-sandbox/kuasar-sandbox/security/advisories/new)。不要在公开 Issue、Discussion 或 Pull Request 中披露尚未修复的安全漏洞。

报告建议包含：

- 受影响的聚合 Release 标签；
- 受影响的组件和精确组件版本；
- 部署模式、架构、存储路径和相关网络拓扑；
- 复现前置、操作步骤和是否需要特定权限；
- 可观察影响和预期安全边界；
- 已去除敏感信息的日志、错误输出或最小复现。

不要提交真实凭据、密钥、客户数据、未公开镜像、内部主机地址或其他敏感信息。请用最小化和可撤销的测试数据替代。

## 处理流程

维护者会先在私密 advisory 中确认收到报告，检查复现条件、影响范围和所属组件。如果问题横跨多仓，项目主仓会在不公开细节的前提下协调相关组件维护者。后续流程包括验证修复、准备所需的组件和聚合发布，并与报告者协调公开时间和可披露内容。

确认前不承诺固定响应或修复时间；复杂度、受影响版本和跨组件范围会影响协调周期。
