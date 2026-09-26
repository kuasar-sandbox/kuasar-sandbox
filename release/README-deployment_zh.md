[English](README-deployment.md) | [简体中文](README-deployment_zh.md)

# Kuasar Release 部署

`kuasar_deploy.py` 用于在满足条件的单台测试主机上准备并验证 Kuasar Sandbox 聚合 Release。

## 支持的主机

- x86_64 Linux
- systemd 和 cgroup v2
- 可读写 `/dev/kvm`
- glibc 2.38 或更高版本
- 调用用户可免密码使用 `sudo`
- 正在运行的 Docker daemon
- 可通过受信任 CA 链以 HTTPS 访问公开 GitHub Release 资产
- 没有占用相同网络资源的 Kuasar demo

请以普通用户运行工具。需要提升权限的操作会由工具通过免密码 `sudo` 执行。

## 快速开始

在仓库根目录执行：

```bash
./release/kuasar_deploy.py quick-start --version release-v0.1.5
```

该命令安装缺失的文档化依赖，检查主机，下载并校验指定 Release，解压二进制文件，启动 demo 服务，并执行官方端到端验证。成功时退出码为 0，输出 `Result: SUCCEEDED`；失败时退出码非 0，并输出失败原因。

仅检查环境：

```bash
./release/kuasar_deploy.py check
```

停止已准备的 demo：

```bash
./release/kuasar_deploy.py stop --release-dir /path/to/extracted-release
```

## 匿名下载

公开 Release 不需要 GitHub CLI、GitHub 账号或 token。工具通过保持 TLS 校验的 HTTPS 下载 `SHA256SUMS`，从中取得归档文件名，并在解压前校验每个资产。企业代理环境应将其 CA 加入系统信任库或设置 `SSL_CERT_FILE`。下载失败后，已校验的缓存资产会复用；私有 Release 不支持此匿名路径。

## Unit 隔离兼容性

`release-v0.1.5` 为每次 demo 运行生成唯一的 runner 和 builder systemd unit 名称，工具会报告 `Unit isolation: per-run`。未来 Release 可能通过 `DEMO_UNIT_PREFIX` 支持可配置名称；工具只会在该变量受支持时传递 `--unit-prefix`。若指定 Release 不支持任何隔离方式，工具会在部署前失败；若 Release 仅支持每次运行隔离，指定自定义前缀也会明确失败。`release-v0.1.2` 不支持该选项，因此不再是本工具的默认版本。

## 验证依赖版本

验证环境在隔离的 `.venv` 中安装经校验 Release 里 `test/demo/requirements.txt` 指定的精确 SDK 依赖。这样既与所选 Release 保持兼容，也不会修改主机 Python 环境。
