[English](sandboxes.md) | [简体中文](sandboxes_zh.md)

# 使用与管理沙箱

本文面向使用已部署平台的应用，覆盖实例创建、命令和文件、应用访问、暂停恢复、显式冷启动、
重置及清理。[概念](concepts_zh.md)解释对象；Orchestrator 的
[受管生命周期](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/sandbox-lifecycle_zh.md)
维护精确 API 字段、状态转换、错误及保留行为。

## 准备连接

向平台运维者取得所选发行版的 SDK、`E2B_API_URL`、`E2B_API_KEY`、`E2B_DOMAIN`、
可信 TLS CA 设置和可用的规范 `TEMPLATE_ID`。下例使用仓库固定的 **e2b==2.25.1**；
发行版使用不同版本时，遵循该版本自带的 pin。完成的 Demo 通常会停止临时节点，
应使用其文档支持的交互会话或另行部署的服务。

集群还需要保存该实例的 `KUASAR_GROUP` 和调用者选择的 route key，后续控制与数据请求
都要携带。Group 的端口约束必须允许目标服务。
[集群客户端契约](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/cluster_zh.md)
维护 provider 设置、路由和 header 要求。仅有 Sandbox ID 不能构成完整集群路由上下文。

## 创建、执行、保存状态并清理

设置上述连接变量。存在 `KUASAR_GROUP` 时，下例生成一个 route key 并在整个过程复用。
应用寿命长于本次客户端进程时，应持久保存打印的 group/route/Sandbox 身份；
重新连接该实例时不要生成新的 route key。

```python
if not __debug__:
    raise RuntimeError("Run this recipe without Python optimization (-O/-OO or PYTHONOPTIMIZE).")

import os, uuid
from e2b import Sandbox

headers = {}
if os.environ.get("KUASAR_GROUP"):
    headers = {
        "X-Kuasar-Sandbox-Group": os.environ["KUASAR_GROUP"],
        "X-Kuasar-Route-Key": str(uuid.uuid4()),
    }
options = dict(api_url=os.environ["E2B_API_URL"],
               api_key=os.environ["E2B_API_KEY"],
               domain=os.environ["E2B_DOMAIN"], headers=headers)
sandbox = Sandbox.create(os.environ["TEMPLATE_ID"], timeout=300, **options)
sid = sandbox.sandbox_id
print("Retain routing identity:", headers, sid)
try:
    result = sandbox.commands.run("printf sandbox-ready")
    assert result.exit_code == 0 and result.stdout == "sandbox-ready"
    sandbox.files.write("/tmp/session-proof", "session-state")
    assert sandbox.files.read("/tmp/session-proof") == "session-state"
    sandbox.pause()
    sandbox = Sandbox.connect(sid, **options)
    assert sandbox.sandbox_id == sid
    assert sandbox.files.read("/tmp/session-proof") == "session-state"
    assert sandbox.commands.run("test -f /tmp/session-proof").exit_code == 0
finally:
    sandbox.kill()
```

成功要求真实命令和文件断言、暂停、连接恢复及清理全部完成。Create/Connect 可以在
Runtime 或 envd 尚未就绪时接受启动，第一次数据操作可能在 starting 路由上等待。
这里的文件断言验证文件系统状态保留，[模板示例](templates_zh.md#first-application)还验证服务可用。要证明内存恢复，应检查暂停前写入且从未持久化到磁盘的已知应用内存值；固定文件或 HTTP 响应本身不能区分内存恢复与应用重启。

同一个模板可以创建多个独立实例，每个实例有自己的身份与写入，删除一个实例不会删除共享模板。

## 检查状态并明确选择策略

以下 helper 针对**你拥有的现有实例**。设置 `SANDBOX_ID`；集群还需设置 `KUASAR_GROUP`
及该实例保存的 `KUASAR_ROUTE_KEY`。它使用 Python HTTPS 客户端的可信 CA 环境，
不关闭证书校验。HTTP 错误会抛给调用方。

```python
import json, os, urllib.parse, urllib.request

api_url = os.environ["E2B_API_URL"].rstrip("/")
sid = os.environ["SANDBOX_ID"]
route_headers = {}
if os.environ.get("KUASAR_GROUP"):
    route_headers = {
        "X-Kuasar-Sandbox-Group": os.environ["KUASAR_GROUP"],
        "X-Kuasar-Route-Key": os.environ["KUASAR_ROUTE_KEY"],
    }
instance_path = "/sandboxes/" + urllib.parse.quote(sid, safe="")

def control(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        api_url + path, data=data, method=method,
        headers={"X-API-KEY": os.environ["E2B_API_KEY"],
                 "Content-Type": "application/json", **route_headers})
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read()
        return json.loads(raw) if raw else None

state = control("GET", instance_path)
print(state["sandboxID"], state["state"])
```

列表行为取决于访问入口：

- **单节点 Conductor：** `GET /v2/sandboxes` 按租户隔离，支持生命周期规范中的
  `state` 过滤、`limit` 和 `nextToken` 参数，下一页令牌在 `x-next-token` 中返回。
  默认列表不包含部分过渡和终止状态，诊断时应显式过滤。
- **集群 Router：** 相同集合路径返回当前 Group 在 Registry 中的
  `ready`、`pausing` 和 `paused` 路由投影，不转发节点的
  `state`/`limit`/`nextToken` 参数，也不提供相同分页契约。
  这些路由状态不是节点的完整生命周期视图。检查已知实例时，使用上面的 ID Get helper，
  携带保存的 Group/route key。详见[集群控制契约](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/cluster_zh.md#router-control-plane)。

默认列表中没有某个实例，不代表它已经被删除。资源、流量及 usage stats 是只读观测，
不能证明应用就绪；其可用性和历史覆盖由
[观测规范](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/observability_zh.md)定义。

### 暂停与恢复选择

| 意图 | 操作 | 对应用的影响 |
| --- | --- | --- |
| 稍后继续捕获的进程 | 使用默认/true memory 暂停，再普通 Connect | 含 S 的状态在兼容条件下恢复内存 |
| 保留磁盘，稍后重启进程 | Pause 指定 `memory: false` | 生成仅含 E 的 paused 状态，之后自动选择的 Connect 冷启动它 |
| 明确从保留磁盘重启 | 对 paused 来源 Connect 指定 `memory: false` | 不恢复旧进程内存 |
| 要求保存的内存 | 对 paused 实例 Connect 指定 `memory: true` | 必须有可用的 S，不会静默降级为冷启动 |

这些 memory 选择用于恢复 **paused** 实例。对 **running** 实例 Connect 是幂等操作，不会因 `memory: false` 而重启进程。要对现有运行实例明确保存磁盘状态再冷启动，先 Pause：

```python
control("POST", instance_path + "/pause", {"memory": False})
control("POST", instance_path + "/connect", {"memory": False, "timeout": 300})
```

随后验证真实命令/文件操作和应用初始化。`memory: false` 不能同时传入仅用于快照的
merge/drop 选项。在生命周期操作释放所有权之前保留完整来源依赖。
跨节点恢复还必须满足[恢复资格](upgrading_zh.md#restore-eligibility)。

### 超时、空闲策略与重置

通过支持的 timeout 操作更新现有实例的寿命：

```python
control("POST", instance_path + "/timeout", {"timeout": 300})
```

TTL、`autoPauseMemory`、显式 Pause 与 `idleTimeout` 是不同策略。
空闲观测来自可信 Proxy 活动，不表示任意 Guest 后台工作都已结束。
条件暂停使用生命周期规范中的 `ifIdle`/`ifIdleSince`，不能把客户端先检查空闲、
稍后无条件 Pause 当作原子操作。

对磁盘仍可用的 **dead** 实例，Reset 将其转换为仅含 E 的 paused 来源，不会启动 VM。
应用能够从磁盘重新初始化时，可以 Reset 后 Connect：

```python
control("POST", instance_path + "/reset")
control("POST", instance_path + "/connect", {"memory": False, "timeout": 300})
```

其他活跃状态可能冲突，当前磁盘无效或不可用时 Reset 可能失败并保持 dead。
`autoReset` 是 Create 时确定的选项，失败信息保留用于诊断。手动和自动 Reset
都不能恢复尚未捕获的内存。Dead 数据仅保留到 Delete 或配置的 `dead_ttl` 到期；查询与失败重试不续期。决定重试、保留证据还是删除实例前，应检查状态、期限和记录的错误。

## 访问应用端口

先在 Guest 中运行服务，或使用[模板](templates_zh.md)示例启动 8080 端口的 HTTP 服务。
公开数据域名必须具备正确 DNS/TLS，Group 端口约束必须允许 8080。使用前面的 helper：

```python
connection = control("POST", instance_path + "/connect", {})
token = connection["forwardAccessToken"]
request = urllib.request.Request(
    f"https://8080-{sid}.{os.environ['E2B_DOMAIN']}/",
    headers={"X-Access-Token": token, **route_headers})
with urllib.request.urlopen(request, timeout=60) as response:
    print(response.read().decode())
```

这里使用经过认证的标准 HTTPS 数据入口。部署使用其他公开端口或入口规则时，
遵循该部署的 endpoint 说明。不要把 API key 放进 Guest 或浏览器 URL。
`get_host(port)` 仅给出主机名，不会为浏览器注入集群 header 或作用域 token。
原生远程 exec 有独立的 ExecAccessToken 流程，见
[Proxy 契约](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-proxy_zh.md)。

## 结束工作与处理失败

不再需要实例保留状态时，删除该实例：

```python
control("DELETE", instance_path)
```

删除将清理责任交给所属控制面。响应成功不代表所有异步目录/记录 finalizer 已经结束，
不要通过直接删除父层制品来强制清理。

控制请求失败时检查所有权、请求状态、准入和来源可用性；控制成功但数据失败时检查
Proxy 注册、Group/route header、token 作用域、DNS/TLS、目标端口和应用就绪状态。
Pause 失败不能证明快照已提交。保留已有来源与第一处错误，恢复与数据保留顺序见[运维](operations_zh.md)。
