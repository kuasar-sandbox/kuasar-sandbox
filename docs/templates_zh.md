[English](templates.md) | [简体中文](templates_zh.md)

# 构建和复用模板

本文面向应用开发者，完成从输入镜像到可复用模板的全过程。前置条件是一个已通过[部署验收](deployment_zh.md#production-start)的独立节点入口，以及该发行版使用的 SDK。集群入口额外遵循 [Group/route 客户端契约](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/cluster_zh.md)，下例展示独立节点流程。

## 选择要发布的状态

| 目标 | 适合的用途 | 使用时发生什么 |
| --- | --- | --- |
| Image | 交付应用文件系统与启动配置 | 实例执行初始化和应用启动 |
| Sandbox E | 复用已经准备好的磁盘状态 | 新运行从 E 冷启动，不恢复进程内存 |
| Snapshot S | 复用已初始化进程和内存 | 在兼容执行环境中恢复捕获状态 |

通过 [Build 精确目标配方](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-build_zh.md#client-workflow)选择显式目标。下例沿用当前 SDK 的 auto 选择并验证返回类型；不要仅凭存在 startCmd 猜测执行阶段。

<a id="first-application"></a>
## 自己的第一个模板和应用

Demo 之后使用运行中的 Node，复用 [Node 接入与 API key 契约](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-proxy_zh.md#sdk-configuration)
已有的 `E2B_API_KEY`、`E2B_API_URL`、`E2B_DOMAIN`、数据入口/DNS 和可信 TLS CA。
Demo 完成时会停止服务；临时凭据和容器 localhost 不会配置独立主机客户端。
交互探索可保留文档中的 Demo `--pause` 会话，或按[部署](deployment_zh.md)准备
自己的 Node。不要把 Demo 凭据用于生产。

本配方面向仓库固定的 **e2b==2.25.1** 和当前
[Build 契约](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-build_zh.md)。
若所选 aggregate 不同，使用其打包指南和 SDK pin；当前源码示例不能认证旧 Release。
把 `APP_IMAGE` 设为自己的 digest 固定、架构匹配的 registry 镜像，需包含 Python 3、
shell 和可写 `/tmp`。按 Node 现有凭据机制配置 registry 认证。先检查
[镜像和内核适配](https://github.com/kuasar-sandbox/guest-runtime/blob/main/docs/flatten-cli_zh.md)。
2 CPU/6 GiB 构建预算沿用 Demo，不是容量推荐。

build 命令准备文件，start 命令运行应用，ready 命令在快照发布前检查应用 HTTP
响应。固定 sleep 本身不能证明应用就绪。非空 start/ready 选择 auto 内存目标。
不要向 `Template.build` 传 register-only 配置头；此 SDK 也会把头传给 trigger。

```python
import json, os, urllib.request
from e2b import Sandbox, Template

api = os.environ["E2B_API_URL"].rstrip("/")
key = os.environ["E2B_API_KEY"]
image = os.environ["APP_IMAGE"]  # registry/repository@sha256:...
start = "python3 -m http.server 8080 --directory /tmp/my-app"
ready = "python3 -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/', timeout=2)\""
tpl = (Template().from_image(image)
       .run_cmd("mkdir -p /tmp/my-app && printf 'my application\\n' > /tmp/my-app/index.html")
       .set_start_cmd(start, ready))
info = Template.build(
    tpl, name="my-first-app", cpu_count=2, memory_mb=6144,
    on_build_logs=lambda entry: print(entry.message, flush=True),
)
request = urllib.request.Request(
    f"{api}/templates/{info.template_id}/builds/{info.build_id}/status",
    headers={"X-API-KEY": key})
with urllib.request.urlopen(request, timeout=30) as response:
    status = json.load(response)
assert status["buildID"] == info.build_id and status["status"] == "ready"
assert status["profile"] == "e2b" and status["kind"] == "snp"
assert status["target"] is None
canonical_id = status["templateID"]
assert canonical_id.startswith("e2b-snp-")
print("Keep canonical template ID:", canonical_id)
sandbox = Sandbox.create(canonical_id, timeout=300)
try:
    assert sandbox.commands.run("cat /tmp/my-app/index.html").exit_code == 0
    sandbox.files.write("/tmp/checkpoint-proof", "preserve me")
    sid = sandbox.sandbox_id
    sandbox.pause()
    sandbox = Sandbox.connect(sid)
    assert sandbox.files.read("/tmp/checkpoint-proof") == "preserve me"
    assert "my application" in sandbox.commands.run(
        "python3 -c \"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/').read().decode())\"").stdout
finally:
    sandbox.kill()
```

预期为精确 build 的 ready 状态、`kind=snp`、canonical `e2b-snp-...` ID、命令与
文件访问成功及恢复后状态一致。保留 callback 输出：SDK 2.25.1 在 trigger/等待前
记录注册 ID 和 build ID，即使后续抛错而不返回 `info`，仍可据此定位。
保存注册/build 句柄用于诊断，canonical ID 用于
后续 create；SDK 返回的注册句柄不是发布 ID。显式 Image、冷 Sandbox E 或内存
Snapshot S 使用[分离注册/触发配方](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-build_zh.md#client-workflow)。

构建失败先查该 build 的状态/日志、镜像拉取、阶段容量及 ready 命令，再考虑重试。
COPY 存储缺失是独立不支持边界，本例不使用 COPY。pause 失败不是快照保留成功。
`kill` 清理实例，不删除模板远端数据；对保存的注册句柄使用
[Build cancel/delete](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-build_zh.md)，
工件遵守[保留流程](operations_zh.md#retention-runbook)。不要为清理失败实验删除父链。
## 复用、更新与清理

1. 保存精确 Build 的 `buildID`、注册句柄和 ready 状态中的规范 `templateID`。注册句柄用于诊断和 Build 清理，规范 ID 用于后续创建。
2. 从同一个规范 ID 创建多个实例，检查各自身份和写入隔离。已有实例不会因后来发布另一个模板而自动更新。
3. 修改应用时构建并验收一个新模板；先验证启动、命令、文件、应用 ready 与所需恢复路径，再切换新建实例使用的 ID。
4. 为回退保留旧模板及其完整父链、密钥和 runtime/kernel 依赖。[升级指南](upgrading_zh.md)说明内存状态的兼容限制。
5. 按 Build Cancel/DELETE 处理构建记录，按[数据保留流程](operations_zh.md#retention-runbook)处理制品。Build 记录清理、实例删除和远端制品回收是不同操作。

## 输入适配与失败处理

使用架构匹配、digest 固定的镜像。先检查镜像中的 shell、用户、文件权限、启动命令、依赖的内核能力和网络客户端工具，参见 [flatten CLI](https://github.com/kuasar-sandbox/guest-runtime/blob/main/docs/flatten-cli_zh.md)。COPY 还需要已配置的上传存储；不使用 COPY 的配方不能验证它。

ready 失败时保留该次 Build 的日志和 ID，区分镜像获取、资源准入、构建命令、应用启动、ready 检查和发布失败。只有精确 Build 的 ready 状态及真实创建验证成功，才能把规范 ID 交给使用者。固定 sleep 不能替代应用 ready 检查。
