# 首次 Tag 源码切换

[English](tag-source-cutover.md)

早于各仓库 `release/guide-inputs.txt` 声明的 owner Tag 不能按新合同组装新的聚合包；从 main 借用声明或内容会产生不真实的来源记录。

首次迁移采用一次明确的发行选择，与 Daily 的产品相关输入差异算法分开：

1. 先合并平台源码合同实现，再合并使用平台 `release/producer-inputs.py` 的组件生产工作流。
   检查正常 owner 源码分支已包含本仓指南声明及全部声明输入。为 accelerator、connector、
   sandboxer、orchestrator 和 runtime 选择正常的新组件 Tag。若已选 vmlinux Tag 的独立
   kernel 指南完整，保持该 Tag 不变。
2. 单独评审选择这些新 Tag 和新聚合版本的清单变更。本机制变更不修改当前版本选择。
   Stable 使用现有 formal coordinator 的明确已提交选择。Preview 对已提交清单调用普通
   组件发行工作流，按依赖顺序先 accelerator/connector，再 sandboxer，最后 orchestrator/runtime。
   请求选择 owner 分支；源码身份和聚合分支身份由 GitHub API 自动读取并由预检核验，
   不要求人工提供 SHA 作为版本或检索键。正常组件 Tag 尚未发布前，不以通用 Daily 选择器
   替代这次明确选择，也不加入因文档差异自动升产品版本的规则。
3. 正常组件 Release 就绪后，运行已评审清单的普通聚合路径，按新选 Tag 获取源码。
   源码检查、本仓指南发现、helper 校验、实际包输入检查和两种架构 gate 均保持必需。
   声明缺失必须在发布前失败。首次新聚合内含完整 validation plan、owner 用例归属及
   来源证据，后续消费者不依赖短期 CI artifact。

此路径创建真实的新组件 Release，不重标记旧源码、不修改历史 Tag 或发布包。
没有通用强制构建开关，也不把指南路径加入产品差异投影。迁移后，普通文档/测试变更
继续复用产品版本，直到一次明确选择或产品变更选择新 Tag。PR 测试仍使用准入候选源码，
产品字节可继续复用。

确定性测试覆盖旧 Tag 缺失声明、禁止借用较新 HEAD、产品输入不变时正常新 Tag 的接受，
以及按分支准入后两种架构恢复相同固定输入。历史独立测试来源保持原样；真实归属证据
无法恢复的历史聚合不能被表示为已经通过 Tag 源码测试。
对于没有准入候选变更的所有者，若所选 Tag 缺少已验证基线中的任何用例 ID，源码 CI
也会明确拒绝。这需要通过明确的新 Tag 迁移解决，不能静默缩减测试覆盖。
