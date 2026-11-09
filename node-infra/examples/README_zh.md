[English](README.md) | [简体中文](README_zh.md)

# node-infra 配置示例

这些文件用于扩展一个已交付的 kuasar-sandbox node-infra 实例。
[部署指南](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/main/docs/node-infra_zh.md)说明宿主前置条件、安装、运维与恢复。
使用展开的部署包时，请阅读随包提供的部署指南。完整配置 schema 由组件仓库维护。

## 多资源池

将 `multi-pool/50-platform.yaml` 挂载到
`/etc/node-conductor/conf.d/50-platform.yaml`，将两个 `.conf` 文件挂载到
`/etc/node-infra/placement/p0.conf` 和 `p1.conf`。示例以外层 8 CPU / 32 GiB
额度为基础，预留管理开销。使用前按实际宿主设置预算以及可选的
`AllowedCPUs`/`AllowedMemoryNodes`；片段没有假设宿主 CPU 或 NUMA 编号。

Runner 与 Builder 列表同时替换。资源池引用和各自模板继续使用 Conductor 原生字段。
如果前层旧配置包含 `units.runner`、`runner_pool_size`、`builder`、
`builder_pool_size` 或旧版物理内存计算字段，切换为显式池时用覆盖层 `null`
删除这些继承字段。服务启动脚本把主配置和排序后的明确文件列表传给原生加载器。

## 客户 xproxy 与 xconductor 镜像

使用 `extensions/` 作为构建上下文：

1. 按所选基础镜像 `/usr/share/node-infra/image.json` 记录的 orchestrator SDK
   版本构建 `out/xproxy` 与 `out/xconductor`。
2. 将 `private/r42/*.yaml` 替换为应用私有配置。示例环境变量名由应用定义，
   应用需要自行读取；它们不是 node-infra 配置项。
3. 使用固定的 `BASE_IMAGE` 构建 Containerfile，再将派生镜像的不可变身份填入实例 Quadlet。

```sh
podman build --build-arg BASE_IMAGE=ghcr.io/kuasar-sandbox/node-infra@sha256:BASE_DIGEST \
  --tag localhost/node-infra-custom:r42 --file extensions/Containerfile extensions
```

在 Quadlet 模板中移除会遮蔽 COPY 文件的配置目录挂载，或改为具体站点文件的单文件
挂载；保留数据与恢复挂载。派生镜像使用带 `sha256:` 前缀的完整配置 ID，构建时使用
的可变 tag 不能作为安装身份。

服务仍启动官方 `node-ctl conductor serve` 或 `node-ctl proxy serve`，由原生
bootstrap 执行所选应用。保留 `/opt/node-infra/bin/` 中的官方程序与相邻 helper。
示例安装 root 持有的可执行文件，并通过具体组件服务上的 `EnvironmentFile` 设置环境。

运行期将相同单文件挂载到相同位置，与 COPY 使用同一套配置接口。固定版本的私有路径
使 Proxy worker 重启时仍读取相同私有配置。启用新版本时，按正常维护流程重启相关服务
或更换实例。

`node-ctl config` 检查声明配置，不执行自定义 Configure/BindRuntime。使用所选
SDK/镜像组合验证真实启动、自定义请求路径和 worker 重启。platform 的
`basic.node-infra-config.sh` 用例通过正式部署入口和仅用于测试的 SDK 应用演示这条链路。
