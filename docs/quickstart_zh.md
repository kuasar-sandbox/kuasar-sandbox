[English](quickstart.md) | [简体中文](quickstart_zh.md)

# 运行第一个 Guest

使用已发布的 **node-infra** 镜像启动持续运行的节点，在真实 MicroVM 中执行命令，再检查或停止节点。首次运行不需要源码构建、Workbench、独立 Store/Cache 服务、Guest 内的镜像仓库访问，也不需要手工制作 overlay 文件系统。

`quickstart` profile 提供小型 Linux shell 文件系统和私有凭证。它面向本地体验：不公开 API/data 端口，也未配置普通 Guest 互联网出口；它提供随包 shell 工作负载，不是完整的站点 Build/SDK 配置。使用自己的镜像、E2B SDK 客户端或集群时，请继续下方的显式站点部署流程。完整 [SDK Demo](../test/demo/DEMO_zh.md) 仍用于开发和发布验证。

## 1. 选择匹配的已发布镜像

使用原生 Linux x86_64 或 aarch64、本地 rootful Docker Engine、cgroup v2、可用的 `/dev/kvm` 和 `/dev/net/tun`，以及网络命名空间、BPF/TC 和 userfaultfd。[主机要求](node-infra_zh.md#前置条件与发布输入) 说明系统容器路径所需条件。Docker Desktop、架构模拟和远程 Docker daemon 不能代替这些主机能力。WSL 2 用户先检查 [Linux 主机与资源要求](../workbench/README_zh.md#主机与镜像)。

此例明确选择已发布、包含当前 node-infra、cluster 和原生 egress 流程的 Preview。Stable 可能选择更早的功能集合，应使用所选版本附带的指南。不要混搭各组件的 Latest。[安装指南](installation_zh.md) 也说明离线归档和原生工具的获取方法。

```bash
RELEASE_VERSION=release-v0.2.0-preview.20261127
IMAGE="ghcr.io/kuasar-sandbox/node-infra:$RELEASE_VERSION"
docker pull "$IMAGE"
docker image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' "$IMAGE"
IMAGE="$(docker image inspect --format '{{.Id}}' "$IMAGE")"
```

保存版本及实际解析出的镜像身份。node-infra 的镜像 tag 包含 `release-`。离线使用时，按 [node-infra 输入](node-infra_zh.md#前置条件与发布输入) 校验并导入匹配的 `.docker.tar.gz`，再将 `IMAGE` 设为导入后的镜像 ID。Docker 导入 Docker 归档；Podman 也可导入独立 OCI 归档。

## 2. 启动节点并执行命令

使用未占用的容器名和新的专用卷。示例为整个节点分配 4 CPU 和 6 GiB，包括管理服务和 Guest；这是体验预算，不是通用容量建议。

```bash
docker run -d --tty --name ks-dev --pull=never --privileged \
  --cgroupns=private --cpus=4 --memory=6g --memory-swap=6g \
  --tmpfs /run --tmpfs /run/lock --tmpfs /tmp:exec --stop-timeout=420 \
  --network=bridge -v ks-dev-data:/var/lib/node-infra "$IMAGE" quickstart
docker exec ks-dev node-infra-run -- /bin/sh -c 'printf "hello from guest\n"'
```

预期输出 `hello from guest`，退出码为 0。`node-infra-run` 等待节点初始化和空闲 Runner，创建新的 MicroVM，在 Guest 内执行参数向量，返回输出及退出码，然后删除该 Guest。打印的 sandbox ID 标识这一次运行。再次执行第二条命令会创建另一个独立 Guest，前一次调用写入的文件不会成为下一次调用的保留会话。

节点持续运行，专用卷保存节点身份、凭证和被引用的基础 Bundle。profile 根据随镜像提供的输入初始化一次，不会在启动时下载工作负载镜像。将卷直接挂载至 `/var/lib/node-infra`，不要增加嵌套挂载。不同节点使用不同卷，恢复时保留对应的制品依赖。

## 3. 检查、停止和重启

```bash
docker exec ks-dev /usr/libexec/node-infra/node-infra-runtime health
docker exec ks-dev node-ctl --version
docker logs ks-dev
docker exec ks-dev journalctl -u node-conductor.service --no-pager

docker stop ks-dev
docker start ks-dev
docker exec ks-dev node-infra-run -- /bin/sh -c 'printf "guest after restart\n"'
```

停止节点会结束运行中的 VM，不会自动捕获所有 Guest 的内存。重启会复用节点的持久输入。命令客户端被强制杀死时无法发送 Delete，应保留其打印的 sandbox ID，通过节点生命周期 API 检查并删除；普通执行完成会自行清理。

结束体验时，先停止并删除容器。仍需数据或日志时保留卷；确定丢弃本次体验后再删除：

```bash
docker stop ks-dev
docker rm ks-dev
# 可选：永久删除本次体验的持久数据。
docker volume rm ks-dev-data
```

## 步骤失败时

| 现象 | 下一步检查 |
| --- | --- |
| 容器或预检失败 | 检查主机 KVM/TUN、cgroup v2 delegation、内核能力及 `docker logs`，使用满足条件的原生主机。 |
| profile 拒绝配置或数据 | 首次初始化使用专用空卷；不要为该 profile 挂载站点 `/etc` 配置，也不要清空现有节点数据来绕过检查。 |
| Guest 命令等待或失败 | 检查内部 health、Conductor/Runner journal 和节点容量；保留第一次错误再重试。 |
| Guest 不能访问互联网 | 此 profile 的预期行为。Docker bridge 连接属于节点网络，不等于 Guest SNAT；在站点部署中显式配置网关。 |
| 旧版本缺少 helper/profile | 使用该版本指南，或明确选择已经交付此流程的版本。 |

## 继续部署自己的应用

使用同一所选版本的镜像和部署包，创建独立的[站点部署](deployment_zh.md#production-start)。配置 API/data 入口、凭证、制品保留、Runner/Builder 池及可选的[原生 egress](node-infra_zh.md#native-egress)。启动时不传 `quickstart`，改为提供原生配置路径。体验 profile 会拒绝站点配置挂载，给它追加 overlay 不是原地升级为应用部署的流程。

接着[构建模板](templates_zh.md)并[使用 E2B SDK](sandboxes_zh.md)。多节点场景按[最小集群](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/cluster_zh.md#minimum-cluster)操作，并为每个实例保存 Group/route 身份。Workbench 的[完整验证流程](../test/QUICKSTART_zh.md)是检验这些部署选择的独立任务。
