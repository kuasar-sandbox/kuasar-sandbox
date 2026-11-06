[English](security.md) | [简体中文](security_zh.md)

# 平台安全边界与配置责任

本文定义平台部署中的信任边界和保护责任。漏洞报告与支持版本政策见根目录 [SECURITY](../SECURITY_zh.md)。精确 token、加密、网络和 MMDS 协议由实现组件维护。

## 信任边界与部署检查

| 边界 | 部署时必须建立的关系 |
| --- | --- |
| 不可信 Guest 与可信 Host | 使用选定的 KVM/VMM/Guest 组合及其隔离接口；保持 host 控制 socket、存储密钥和管理网络不可被 Guest 任意访问 |
| 平台用户与 API | 为公开入口配置 TLS、凭据验证及租户所有权；不能把 Demo 的临时凭据当作生产身份 |
| 业务访问与远程执行 | 按目的签发和传递数据/exec 能力；API 根凭据不能用作发给 Guest 的应用端口 token |
| 节点与集群内部服务 | 使用规定的 node-link 身份验证，并保护 Router 到节点的内部网络；公开 HTTPS 不自动保护其他连接 |
| 内容共享域 | 由内容密钥、命名位置和后端访问权限确定共享范围；相同字节不构成跨租户授权 |
| Guest 出站与管理服务 | vSwitch 建立可信端口身份，外部网关执行所选策略，MMDS 仅暴露 Conductor 授权的路由与已注册本地服务 |

当前集群 Router 到节点的 APIEndpoint/DataEndpoint 使用内部明文 HTTP/CONNECT；应隔离该网络，公开 TLS 在 Router 或前置负载均衡处终止。node-link 的 mTLS 独立配置。单节点直接 TLS 监听与集群内部监听不能直接混用，见[部署](deployment_zh.md)。

## 多租安全

- 平台身份凭据与内容保护密钥用途分离.APISecret 用于平台身份和控制面验证,
  ManifestKey 保护内容密钥域.
- 控制面,普通数据面和远程执行使用不同作用域的访问凭据;远程执行 token 绑定
  具体 subject,用途和条件.
- 内容保护密钥留在可信 host 数据路径,不注入 Guest.
- 本地和远程工件可以按部署策略加密和校验;密钥保护,备份和轮换仍是部署责任.
- 去重和共享范围由内容密钥与安全域配置决定.内容相同不代表应该跨租户共享.
- 暂停,恢复和迁移后,稳定 Sandbox ID,凭据 binding 和可信网络身份边界继续成立.
- 网络隔离,节点资源准入和故障域与数据加密共同构成多租安全,不能由单一机制替代.

凭据更新只影响之后创建并复制凭据的业务记录.已持久化到现有沙箱记录的凭据保持其
原 binding,不会因 key-distribution 表更新自动重绑.

## MMDS Route 安全与部署边界

Conductor 配置拥有 `mmds.listen`、路由准入策略与本地服务注册。具体沙箱或 Build 路由由受支持请求提交，经 Conductor 按策略校验并投影。开放新沙箱准入前先启动并注册独立 Proxy；Create 依赖其 route barrier。服务工作负载前还须验证 data/MMDS 监听与路由。在配置的网络 namespace 中绑定 MMDS listener，并将 vSwitch 管理流量指向它。本地服务使用显式配置的绝对 Unix socket URI，不向 Guest 暴露宿主 socket。

[Proxy MMDS 契约](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-proxy_zh.md#mmds) 统一定义请求身份、路由匹配、secret 处理、Header 过滤和转发限制;[Build](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-build_zh.md) 维护构建作用域注册与终态清理。公共 API 的 TLS/鉴权、本地 socket 权限、Guest 隔离网络与服务凭据需一并配置，不能在 Proxy YAML 再维护一份服务注册表。

<a id="key-incidents"></a>
## 密钥事故

怀疑密钥泄露时，停止用相关凭据新发布，通过部署现有控制限制访问并保存事故与
依赖证据，协调应用/密钥/存储 owner。已有 Node 行保留绑定凭据，移除 allowlist/
provider 不会追溯撤销。不要假设 Manifest 原地轮换或远端密文撤销能力。按支持
流程生成并验证替代工件/凭据，有计划更新消费者；只要旧快照仍需保留，就安全
保存旧密钥。密钥丢失时从批准备份恢复，否则不能声称加密工件可恢复。不要用
purge 旧数据修复密钥事故。

## 验收与保留

验证无凭据、错误租户、错误 Group/route key、未授权端口和未允许 MMDS 路由被拒绝；同时验证获授权的真实业务能完成。密钥标识可以进入证据，密钥内容不能进入日志或共享报告。数据与密钥退役遵循[完整保留根检查](operations_zh.md#retention-runbook)。
