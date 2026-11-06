[English](upgrading.md) | [简体中文](upgrading_zh.md)

# 版本升级与恢复兼容性

本文面向维护平台版本的运维者。它定义组合选择、恢复资格和升级操作；E/S 格式、VMM/Guest ABI 与制品绑定仍由组件规范定义。共享存储可达不能单独证明一个目标节点能恢复已有实例。

## 选择需要保留的状态

| 路径 | 保留什么 | 升级前的验证重点 |
| --- | --- | --- |
| 从模板创建新实例 | 模板内容，新的实例身份 | 目标版本能读取该模板并完成启动、应用 ready 和数据访问 |
| 从 S 恢复内存 | 进程、内存和磁盘的捕获状态 | CPU/架构、VMM、kernel/runtime、格式、完整父链及节点所有权均兼容 |
| 从 E 冷启动，或显式从 S 选择冷启动 | 保存的磁盘状态，重新启动进程 | 目标版本支持 E 读取和所需配置覆盖；应用能重新初始化；不承诺任意替换捕获绑定 |
| 重新构建模板 | 经明确迁移的应用数据和新软件环境 | 使用新输入完成 Build/ready/创建验收，并单独处理业务数据迁移 |

对 paused 实例的 `connect(memory=false)` 是明确放弃旧进程内存的启动选择，不是绕过格式、身份或依赖校验的方法。对 running 实例 Connect 保持幂等，不会重启；需要重启时先按生命周期规则 Pause。无法证明 S 兼容时，应选择经过验收的冷启动或重建方案，不修改快照元数据伪装兼容。

## 升级与回退流程

1. 记录当前聚合 Tag、组件/Runtime/VMLinux 版本、配置、运行实例、paused 根、模板与回退保留期限；明确每项业务需要内存继续还是允许进程重启。
2. 在新目录按[安装指南](installation_zh.md)准备完整目标组合，保留旧目录、密钥、制品和不可变 runtime/kernel 输入。不要覆盖仍被活跃或保留状态引用的文件。
3. 在隔离节点验证新建实例、Build、命令/文件/端口、暂停、S 恢复和需要的 E 冷启动；同时验证实际网络、存储、架构和 CPU 组合。
4. 集群通过现有 Group/provider/selector 管理目标节点资格。先停止向待维护节点分配新工作，再处理其已有实例；“节点在线”不等于“兼容所有旧快照”。
5. 对需保留状态的实例完成受支持的 pause/export/import 或原地保留流程，确认成功提交与完整依赖，再按消费者到提供者的顺序停服务。SIGTERM 不等于自动为所有 VM 保存快照。
6. 启用目标组合并按业务路径验证，逐步恢复准入。记录失败、跳过检查和剩余旧版本依赖。
7. 若验证失败，停止新放置，恢复仍兼容的旧配置和程序输入。新版本已写出的格式不能假定旧版本可读；不兼容数据需使用保留的旧根或受支持的导出/迁移流程。

完成迁移和回退保留期之后，才按[保留流程](operations_zh.md#retention-runbook)回收旧依赖。该顺序是操作指导，不宣称当前系统具有自动滚动升级或任意版本间实时迁移能力。

## 版本与验证

组件有独立版本与 Release。主仓聚合版本选择精确的六个发布单元：四个组件 archive、Guest runtime 和 Guest kernel。聚合准备校验所选组件资产及 SHA256，验收按各架构声明的 profile 测试 staged bytes。发布包记录精确版本组合，避免混用不同日期或聚合版本的资产。

验证结论限于实际 profile：原生 x86 执行要求的 KVM 用例，原生 ARM 执行其声明的 profile 并明确记录硬件排除项。托管 ARM Workbench 的 `artifact-only` 结果不能证明 VM 或离线系统执行通过。升级前须将记录的架构、profile 和已执行用例与目标部署核对，见[发行资产验证](release_zh.md#8-发行资产验证)。

这些机制支持生产部署中的版本固定,故障定位和可重复验证.部署方仍需为 TLS,持久化
后端,备份,监控,网络策略和容量配置生产级实现.

<a id="restore-eligibility"></a>
## 跨节点恢复资格与故障边界

portable ref 描述内容，不认证任意目标。部署方放置前必须验证：

- **架构/CPU**：相同原生 Guest 架构，目标特征兼容捕获 VM。验证实际源/目标
  CPU 组合，架构标签和共享 Store 均不构成兼容证据。
- **VMM/快照格式**：配套 patched VMM 与 sandboxer 格式兼容。
  [工件兼容性](https://github.com/kuasar-sandbox/sandboxer/blob/main/docs/artifacts_zh.md#compatibility)
  拒绝旧 schema，不会静默迁移或冷启动。
- **kernel/runtime**：部署可信匹配 kernel 及 runtime 绑定。restore 的廉价
  kernel 检查不重算字节 hash；runtime basename/footer digest 必须匹配 E。
  保留不可变版本输入，遵循[runtime 选择](https://github.com/kuasar-sandbox/guest-runtime/blob/main/docs/using-guest-runtime_zh.md#节点升级)。
- **完整持久闭包**：S、E、memory/disk 父链及不可变对象能按原 canonical identity/
  命名 location 解析。须从目标节点验证，不依赖热 cache。
- **密钥/所有权**：保留每个加密层的客户密钥及 Node/集群要求的凭据/所有权状态。
  共享密文而无密钥不可恢复。
- **网络/资源准入**：provider/topology、当前 attachment generation 契约兼容，
  网关回程/策略及 Proxy 路由有效。恢复保留捕获 capacity/device topology；
  目标 allocatable 策略和准入仍生效。portable 工件不是 reservation，也不允许
  复用旧主机 TAP FD。

[Sandboxer 恢复](https://github.com/kuasar-sandbox/sandboxer/blob/main/docs/runtime_zh.md#restore)
及 [Node 工件生命周期](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/sandbox-lifecycle_zh.md#capture-and-pause)
拥有检查和可覆盖字段。以文件/进程状态、热集外读取、网络往返及外部连接重建验证。
任何 preflight 失败都停止放置、保留工件并修复绑定，不改捕获元数据强行通过。

| 事故 | 可恢复与不可恢复内容 |
|---|---|
| Proxy/Router/connector serve 进程崩溃 | 所有权与底层资源存活时可按契约重连/重开；准入前验证同步、身份、流量，不是 VM checkpoint 操作。 |
| Sandbox/VMM 进程或整机丢失 | 未捕获内存丢失；残存本地状态不自动成为完整 portable checkpoint。仅恢复成功提交且资格/所有权合格的工件。 |
| 原节点不可用，但此前已暂停 | 仍需完整持久闭包、密钥、绑定、网络、准入及对应 Node/集群所有权协议；共享存储仅满足一项。 |
| Registry 执行状态丢失 | 当前协议不能从 route events 重建 ownership 或导入备份执行行。[Registry 恢复限制](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/cluster_zh.md#registry-state-ownership-and-disaster-recovery-boundaries)描述未实现 bootstrap；provider 或 portable 快照不授权伪造 Registry 行或承诺同 ID 恢复。 |
