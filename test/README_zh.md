[English](README.md) | [简体中文](README_zh.md)

# 测试组织

产品 E2E 遵循唯一契约：**预构建制品 → prepare → `<suite>.<case>.sh` → 公共 runner**。完整文件名就是用例 ID，第一段就是 suite。九个 suite 为 `basic`、`storage`、`image`、`network`、`sandbox`、`snapshot`、`orchestrator`、`builder`、`telemetry`。

组件在自己的 `test/e2e/cases/` 和 `test/e2e/lib/` 维护用例及底层 helper。主仓维护 `test/e2e/platform/cases/basic.demo.sh`。源码组装阶段从精确测试 revision 复制用例到扁平的 `test/e2e/cases/`，helper 放入 `test/e2e/lib/<owner>/`。重复 ID、未知 suite 和 owner runner 都会被拒绝。维护归属不影响公共选择语义。

platform 包携带公共 `test/e2e/e2e` runner 和两个架构的预构建 helper。更早的 source/release build 按 `test/demo/requirements.lock` 获取完整 Demo SDK wheel 依赖闭包，并打包两种架构的 wheelhouse。prepare 解析镜像，仅从本地 hash-locked wheel 安装 SDK（`--no-index --find-links`、`--require-hashes`），记录包名/版本/wheel 身份、安装文件摘要、权限和镜像内容 ID。wheel 缺失或变化时失败。执行只消费不可变工作区，不构建产品或 helper、不发现兄弟源码树、不拉取替代镜像、不使用宿主机 helper。被测 Build、flatten、snapshot 和发布操作仍真实执行。

发布包包含规范用例、显式列出的运行库与实际 Demo 输入。源码单元测试和性能工具保留在源码树中。所选双语用户指南位于 `guide/`，accelerator 与 guest-runtime 的 E2E README 双语文件仍位于 `test/e2e/<owner>/`。轻量宿主机启动器及说明位于 `workbench/`。helper 与 wheelhouse 保留原有契约。

```bash
python3 /release/test/e2e/e2e list --suite storage
python3 /release/test/e2e/e2e prepare --release-dir /release --workdir /tmp/kuasar-prepared --suite storage --exclude storage.obs.sh
sudo python3 /tmp/kuasar-prepared/test/e2e/e2e run --workdir /tmp/kuasar-prepared --suite storage --exclude storage.obs.sh
```

`--suite` 与 `--include` 取并集，`--exclude` 按完整文件名排除。未知选择和空集会失败。`--all` 包含需要凭据的 `storage.obs.sh`，公共 CI 明确排除该用例。没有 owner、tag、capability 或 fixture graph 选择器。Makefile 包装入口要求 `RELEASE_DIR`、全新的 `E2E_WORKDIR`，可选 `E2E_ARGS`。

prepare 接受 `--deps-dir` / `E2E_DEPS_DIR` 指定已验证的本地镜像归档，以及 `--offline` / `E2E_OFFLINE=1` 禁止下载依赖。未配置时保持在线行为。本地匹配项验证时不向远端检查新鲜度，无效匹配项也不触发远端修复。外部请求来自单一用例/架构函数，构建期输入收集也可调用该函数。优先级、身份证据与失败行为见[本地及离线准备](QUICKSTART_zh.md#本地及离线准备)。

CI 解析精确测试文件名，并按变更 owner 选择覆盖较广的 suite。源码构建先于 prepare 完成。产品保留自己的源码依赖闭包，测试脚本和 helper 使用独立测试 pin。CI prepare 在无 Go/Rust 工具链和组件源码树的隔离运行环境中执行。每个 suite shard 调用同一打包 runner；storage、snapshot 及 ARM image 验收也使用隔离运行环境，prepare 和执行镜像 ID 及环境检查绑定到结果；结果必须包含所有已选用例的成功退出及一致的不可变输入身份。ARM 选择 accelerator 和 guest-runtime 的非 KVM 用例，排除项明确记录。static lane 执行零个产品用例，不算产品 E2E 验收。

unit、race、vet、源码 helper、UFFD 性能及 working-set 门禁保持独立。`test/perf/warmpool-dedup.sh` 在 `make perf-warmpool-dedup` 下保留 warm-pool 表征、数据与测量断言，不属于 correctness suite。组件源码门禁仍由各自仓库维护。

主仓的重要断言归属如下：

| 契约 | 维护入口 |
| --- | --- |
| 拒绝外部监听者且不杀进程；精确 owner reset | `basic.demo.sh` 及 Demo safety helper |
| 重复 prep、root 所有权、0700 目录和 0600 状态 | `basic.demo.sh` |
| 真实 Build/COPY、Quick Start 生命周期、SDK exec/files、模板 fan-out、迁移与数据 | `basic.demo.sh` 调用文档中的 `test/demo/demo_e2b.sh` 流程 |
| stop/start 持久状态、过期 socket、清理失败状态 | `basic.demo.sh` 及 Demo process/state 源码测试 |
| 精确 wheel-only SDK、隔离导入和不可变输入 | 公共 prepare 与 `test-e2e-tool-paths.py` |
| 有界恢复诊断、脱敏、原始退出/清理语义、描述符关闭 | 主仓诊断 helper；`snapshot.read-recovery.sh` 与 `orchestrator.cluster-recovery.sh` |
| warm-pool dedup 工作负载与测量 | `test/perf/warmpool-dedup.sh` |

发布输入、前置条件和验收证据见 [QUICKSTART_zh.md](QUICKSTART_zh.md)。
