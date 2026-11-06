[English](operations.md) | [简体中文](operations_zh.md)

# 日常运维、恢复与数据保留

本文面向部署运维者，负责健康检查、故障诊断、恢复和数据保留。前置条件是已完成[部署验收](deployment_zh.md#production-start)，并保存所选版本、有效配置与数据所有者记录。日志与指标字段由 [Orchestrator 观测规范](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/observability_zh.md)定义。

## 日常健康与诊断顺序

1. 分别检查控制 API、Proxy 数据入口、节点资源准入，以及已选用的 Store/Cache、共享文件和网关；一个进程存活不代表所有路径就绪。
2. 从失败请求记录稳定 Sandbox ID、RunID、节点、Group/route key（集群场景）、操作和时间范围。使用相同身份关联生命周期状态、日志、原生 usage 和 Telemetry。
3. 先检查控制状态，再做只读统计查询。读取 stats/metrics 不会自动唤醒沙箱；需要验证恢复时，再发起经过授权的业务操作。
4. 用一次命令/文件往返和实际应用响应验证运行状态；端口存在或 socket 文件存在均不能替代应用验证。
5. 记录第一处失败、发生时的版本与配置；先保留证据，再处理自己管理的实例或服务，避免用全局清理掩盖原因。

在节点上先确认当前 RunID 及其 pool 使用的有效 unit 模板，再查询对应 runner 日志。以下示例采用默认 `sandbox-runner@.service`；使用自定义模板或多个 pool 时，按实际模板与 RunID 构造 unit 名称：

```bash
RUN_ID="<current-run-id>"
journalctl --unit "sandbox-runner@$RUN_ID.service" --since "15 min ago" --no-pager
```

## 恢复与故障域

- `sandbox-ctl`,VMM,node,registry,router 和 placer 是可独立观察和恢复的进程/故障域.
- 节点持久记录与运行态 Inventory 用于重启恢复和孤儿清理;host socket 存在本身不是
  Guest ready 证据,恢复必须通过实际健康或执行操作确认.
- 本地工件支持节点亲和恢复；共享文件或 Manifest 使制品可供其他节点读取。恢复仍要求已有 ownership、兼容输入与目标准入。原节点已被判断为 gone 的集群恢复还要求调用方持有有效 MigrationToken，并满足[恢复资格](upgrading_zh.md#restore-eligibility)，不能仅凭快照可读推断会自动迁移。
- registry 保存集群执行态,node 是运行中 sandbox 的事实来源;placer 只给出放置建议,
  最终资源确认仍在 node admission.
- group 是集群分区键,路由,放置,凭据验证和操作保持 group-scoped.

## 故障域

| 故障 | 直接影响 | 恢复方式 |
|---|---|---|
| 单 compute node `store-ctl` 崩溃 | 本机 Manifest origin read/write 停;L1/L2 命中和文件路径不受影响 | systemd 重启后恢复 origin 访问 |
| 单 compute node `cache-ctl tiered` 崩溃 | 需要 wire 的新 fault 按客户端 timeout/cancellation 等待或失败 | systemd 重启;持久的 RocksDB 数据可恢复 L1 命中,miss 仍正常下穿 |
| 单 `cache-ctl shard` 节点崩溃 | 有效 RS 4+1 下,选中的 5 片中任意 4 个不同且有效的 shard index 可用于重建 | systemd 重启;peer 不可达不会自动修改 Maglev 成员表,也没有额外第 6 个 parity peer |
| 同 RS 组中 ≥ 2 `cache-ctl shard` 同时崩溃 | 部分 `(chunk, idx)` 落到 ≥ 2 故障 peer 上,该 chunk L2 miss | 读路径 fallthrough origin(慢但正确);避免方式:成员变更**一次只动 1 peer** |
| `node-ctl` 崩溃 | 北向 API 中断,新沙箱无法拉起 / admit 失败;存量沙箱保持上次 grant 继续跑(资源仲裁随进程在本机) | systemd 重启;状态在 sqlite(`db_path`,默认 `<base_root>/node-ctl.db`)持久化;资源由 inventory/report 重建,已弃用的 state_path 被忽略；以有效 runner 模板集合对应的存活单元（默认 `sandbox-runner@*.service`）对账 sqlite `sandboxes` 表重挂(active+running⇒adopt 重武装 TTL;running 无单元⇒标 dead;`paused`/snapshot 记录保留可被 connect/auto-resume 拉起) |
| S3-compatible 后端不可达 | 使用该 origin 的 Manifest 读写受影响 | 已 L1/L2 命中的沙箱继续跑;依赖新 origin 的写路径 / cold-image fault / 展平上传失败;本地或共享文件路径不受影响 |
| compute node 整机故障 | 该节点运行中的沙箱中断 | 隔离故障节点；集群恢复必须保有 ownership、调用方有效 MigrationToken 及符合条件的目标节点，并验证[完整恢复资格](upgrading_zh.md#restore-eligibility)。共享制品只解决数据可达性；仅本地制品仍依赖原节点/存储，不承诺自动恢复 |
| L2 cluster > parity 同时故障 | 相关 L2 读取 miss | tiered fallthrough origin(slow path 持续);恢复后自然恢复 |

### 资源池恢复

调整池名、unit 模板或容量前，先核对存活执行与资源池身份。重启会在开放准入前重建执行槽位与声明容量占用；降低上限不会让存量实例退出记账。仍有存活执行时，必须保留对应资源身份与 unit 模板配置。历史存活 unit 被多个逻辑池共享时，可能无法恢复其逻辑池归属；启用有限执行槽位前，先排空这类实例。

使用 `node-ctl resource pressure` 区分池的原始压力、待履行恢复义务和目标执行供给不足。因平衡而暂停的沙箱，在没有合格目标时可以继续保持 paused。重试前核验完整内存预算、startup 余量与目标供给；仅凭准入拒绝不能判定工件损坏。精确记账和恢复条件由[资源池恢复契约](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-resource_zh.md#resource-pools)维护。

## 集群故障域

| 故障 | 影响 | 恢复方式 |
|---|---|---|
| 单个 `registry` 成员崩溃 | 其参与的逻辑分片降一格;quorum 仍满足时继续服务,不足时该分片停写 | 成员恢复后通过 quorum read / read-repair catch-up;已路由会话可在无需新 registry lookup 时继续,仍取决于既有 node/transport 连接 |
| registry 整集群完全下电但 shard 数据保留 | 下电期间 Reserve/Place 不可用 | registry quorum 恢复后读取原 shard 数据,node-link 重连并继续收敛 |
| registry 执行 shard 不可恢复地丢失 | 不得从备份构造 sandbox/build 节点执行态 | provider 数据按其持久化流程恢复;存活 node 的执行投影恢复和 migration-token 持久 route 分别由 [orchestrator #34](https://github.com/kuasar-sandbox/orchestrator/issues/34)/[#33](https://github.com/kuasar-sandbox/orchestrator/issues/33) 跟踪,完成前须明确报告不可恢复 |
| `router` 崩溃 | 该副本连接断 | 无状态,LB 改路由其余副本 |
| `placer` 崩溃 | 该 placer 不再作为 ready 候选;冷放置 failover 到同 group 的其他 placer | 热路径不受影响;Place 超时后 registry 换下一个候选 |
| 单 compute 节点 node-link 失联 | registry 暂失该节点视图 | 节点重连重报;node_dead_after 后 node_list 失效,placer 不再放置到该节点;孤儿 sandbox 按 group+sandbox_id 清理 |

## 恢复后的验收

按受影响路径检查路由同步、身份与凭据、资源准入、冷数据读取、命令/文件往返和应用结果。需要恢复进程状态时，额外检查暂停前设置的进程内状态；仅磁盘文件相同不能证明内存恢复成功。只有完成所需检查后再放开准入。

节点迁移和版本更换先核对[恢复资格](upgrading_zh.md#restore-eligibility)。清理测试实例应通过所属控制面删除；不要直接删除其父层或共享制品。维护停机的顺序见[部署](deployment_zh.md#启停依赖)。密钥事故处理见[安全](security_zh.md#key-incidents)。

<a id="retention-runbook"></a>
## 保留与 generation 退役

应用/平台 owner 提供保留根清单：canonical 模板 ID、暂停快照、活跃实例、回滚/
备份根、Build 行过期后仍需保留的输出及保留期限。runtime/storage 运营方按组件
检查契约解析各根完整 E/S 内存/磁盘父链及不可变 kernel/runtime 绑定。Store 不能
从流量或最新 generation 推断该清单。[Store 退役](https://github.com/kuasar-sandbox/accelerator/blob/main/docs/store_zh.md)、
[Manifest 密钥](https://github.com/kuasar-sandbox/accelerator/blob/main/docs/transfer_zh.md)
和 [Node 凭据生命周期](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-proxy_zh.md#credential-model)
是权威契约；这里是决策顺序，不新增 GC 协议。

| 删除项 | 删除前必须提供的证据 |
|---|---|
| Build 行 | 按 Cancel/DELETE 完成 owner 清理；另存所需 canonical ID。删行不删远端工件。 |
| 本地工件/父链 | 完整保留根/父链清单、无 live owner 依赖；若仍需保留则验证替代持久源。语义 alias 或仅叶节点清单不足。 |
| Store G1 | 无保留根/父链只依赖 G1 对象。G2 rollout 只改默认写入；列表内 G1 仍可显式准入。物理删除前协调所有读写方并排空 inflight。 |
| 旧 runtime/kernel | 所有活跃 VM、模板、快照、父链及回滚路径均不再依赖该身份，保留绑定可成功恢复。 |
| 旧客户密钥 | 无保留加密工件依赖，或所有目标替代物已另行生成且完整恢复验证，保留其闭包。新 API key/generation 不会重新加密旧数据。 |

退役前冻结删除集、协调新增发布/保留变化，从冷 reader 检查持久源并测试代表性
保留根恢复。记录 refs、identity、generation、密钥标识（不含 secret bytes）、
owner 和结果。清单不全、任何读取/恢复失败、写入仍竞争或密钥来源不明时，
**停止并保留数据/密钥**。`purge --generation` 同时移除列表和物理删除，不是
引用感知 dry run，不能用来试探 G1 是否还有依赖。批准退役后再次验证保留根，
按运营方恢复策略保留备份。
