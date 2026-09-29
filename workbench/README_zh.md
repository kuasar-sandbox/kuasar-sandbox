[English](README.md) | [简体中文](README_zh.md)

# Workbench

Workbench 为选定的 Kuasar Sandbox 发布版提供原生 Linux 构建工具和系统环境。
同一个镜像支持普通 UID 构建，以及以 systemd 为 PID 1、包含私有 Docker/containerd
的环境。镜像包含外部测试镜像归档；产品、测试辅助程序和 Demo wheel 则来自匹配的
发布版。选择测试请参阅[公共 E2E 指南](../test/README_zh.md)，完整示例请参阅
[Demo 指南](../test/demo/DEMO_zh.md)。

## 主机与镜像

使用原生 x86_64 或 aarch64 Linux、Docker Engine 和 Python 3。启动器使用本机的
Unix Docker 端点。Docker Desktop、远程 Docker 端点、架构模拟、Podman 和 runner
注册不属于 V1 范围。

系统模式要求 rootful Docker、支持 cpu/cpuset/memory/pids/io 委派的 cgroup v2、
4096 字节主机页、KVM 和 TUN 设备、userfaultfd、BPF 以及 overlay2。构建模式不需要
KVM 或额外 capability。调用启动器的普通 UID 必须能访问 Docker，并拥有可写的源码
检出和实例目录；Docker 访问权限本身就是主机信任边界。

在 AppArmor 主机上，系统模式需要主机 `apparmor_parser`，以及 root 或非交互式
`sudo` 权限，以加载/移除唯一的实例策略。启动器记录策略名称、版本和内容哈希，
以 enforcing 模式添加，并验证外层 PID 的实际上下文。它不会替换 `docker-default`
或其他主机策略。无法管理或强制执行自己的策略时，启动失败并保留诊断。构建模式
不加载策略，也不增加权限。主机模块、sysctl 和服务保持不变。主机 `check` 成功仅
表示初步检查通过；`start` 会实际验证私有服务、命名空间、委派及设备/系统调用接口。

通过选定发布版的 registry 标签获取镜像，或验证 `SHA256SUMS` 中的条目后，用
`docker load` 导入本机架构的 `workbench-<arch>-v*.tar.gz`。这是 gzip 压缩的 Docker
镜像归档，不能解压到产品发布树。registry 标签为
`ghcr.io/kuasar-sandbox/workbench:vX.Y.Z[-preview.YYYYMMDD[.N]]`，即聚合版本去掉
`release-` 前缀。新 `workbench-v1` 发布必须包含两个原生镜像归档；历史发布保留原有资产。

## 使用普通 UID 构建

按现有[构建入口](../Makefile)准备自己的六仓库检出。将实例状态放在检出目录之外。
令 `IMAGE` 为已获取镜像的标签或 ID，`SOURCES` 为这些检出的父目录，`STATE` 为当前
UID 拥有的新目录：

```sh
python3 workbench/workbench check --image "$IMAGE" --mode build
python3 workbench/workbench --root "$STATE" --name build start \
  --image "$IMAGE" --mode build --source "$SOURCES" --cpus 2 --memory-gib 8
python3 workbench/workbench --root "$STATE" --name build exec -- \
  make -C /src/kuasar-sandbox build
python3 workbench/workbench --root "$STATE" --name build stop
```

该模式使用调用者的 UID/GID，移除全部 capability，不开放额外设备，并使用只读容器
根目录。它不会 chown 源码检出。构建输出遵循现有 Makefile；`/build`、`/output` 和
`/work` 也都是私有可写挂载。Go/Cargo 缓存和 HOME 属于该实例。镜像提供 Go、
Rust/Cargo、C/C++、clang/LLVM、静态和动态开发库以及内核构建依赖。未来源码版本仍
可能下载模块、crate 或原生依赖源码；不承诺任意源码构建完全离线。

## 准备并运行发布测试

按照发布说明，把选定的原始产品归档和 platform-release 组装到独立的发布目录。
令 `RELEASE` 指向该目录，`ARCH` 按本机架构取 `x86_64` 或 `aarch64`：

```sh
python3 workbench/workbench --root "$STATE" --name e2e start \
  --image "$IMAGE" --inputs "$RELEASE" --cpus 2 --memory-gib 8 --network none
python3 workbench/workbench --root "$STATE" --name e2e exec -- \
  python3 -B /inputs/release/test/e2e/e2e prepare \
  --release-dir /inputs/release --workdir /work/prepared --arch "$ARCH" \
  --suite image --suite storage --exclude storage.obs.sh \
  --deps-dir /opt/workbench/deps --offline
python3 workbench/workbench --root "$STATE" --name e2e exec -- \
  python3 -B /work/prepared/test/e2e/e2e run --workdir /work/prepared \
  --arch "$ARCH" --suite image --suite storage --exclude storage.obs.sh
```

这些套件是原生 ARM 的最小示例。发布验收还在 ARM 执行 `sandbox.lifecycle.sh`、
`snapshot.restore.sh` 和 `network.tapfd.sh`；在两个命令都加入对应的三个 `--include`
选项可覆盖该范围。在 x86_64 上，prepare 和 run 都应使用当前完整
普通选择 `--all --exclude storage.obs.sh`。不得删除用例架构检查。需要凭据的 OBS
仍由用户显式选择，并需要相应网络和凭据。

`--network none` 移除外层实例的外部网络，私有 Guest、Registry、Store 和 Proxy
通信仍然可用。镜像默认设置 `E2E_DEPS_DIR=/opt/workbench/deps` 和 `E2E_OFFLINE=1`。
若有意在以 `--network bridge` 启动的实例中在线准备，请使用
`exec -- env E2E_OFFLINE=0 python3 ...`，不要加 `--offline`。只传递命令所需的任务
变量。离线模式控制依赖获取，不选择用例。缺失或损坏的必要输入会失败；辅助程序和
锁定 wheel 始终来自选定的 platform-release，不回退到 PATH 或全局 Python 包。

## 生命周期与隔离

不带命令的 `exec` 打开 shell。`stop` 请求 systemd 停止私有服务，最多等待 60 秒；
未能正常停止视为错误。对已停止的实例执行 `start` 会使用记录的容器、挂载和资源
预算，并要求原镜像和模式。更改这些设置请使用新名称。`cleanup` 仅停止并移除记录
的容器、网络和守护进程数据，保留 work、build、home、journal 和 output 目录。
显式添加 `--delete-output` 才会同时删除这些目录。所有权记录作为证据保留；清理后
请使用新的实例名称。
仅在移除容器之后才移除其拥有的 AppArmor 策略；若其他容器使用同名策略，则拒绝移除。

每个系统实例具有独立的 machine ID 和 PID/UTS/IPC/mount/network/cgroup 命名空间、
私有 bpffs、Docker 和 containerd 根目录与状态、套接字以及只读发布输入。除 Docker
默认 capability 外，它获得 SYS_ADMIN、NET_ADMIN、SYS_PTRACE 和 KVM/TUN 设备。
保留的默认 seccomp 策略仅额外允许该环境需要的 userfaultfd、pivot_root 和 keyctl
操作。实例不会获得主机 Docker 套接字、主机根目录、整个主机 cgroup 树或主机 BPF pin。

外层 AppArmor 策略允许私有 mount/cgroup/bpffs 和嵌套 Docker 操作，同时保留
proc/sys/firmware/securityfs 限制。在这个已受约束的挂载命名空间中，只读 null
绑定挂载遮蔽 `/sys/module/apparmor/parameters/enabled`，阻止内层 dockerd 尝试
管理主机 AppArmor 策略。这不会关闭主机 AppArmor：内层进程继承外层 enforcing
策略，没有策略切换或 securityfs 管理权限。

CPU 亲和性从调用进程可用的 CPU 中选择，并设置明确的 CPU、内存和进程数限制。
空闲空间检查是容量检查，不是磁盘配额。实例仍共享主机内核、磁盘和 NIC 竞争。该
环境用于可信构建/测试任务，不适用于恶意 root 多租户或独立性能测量。

## 维护者构建与验证

`collect-inputs.py --cases <assembled-case-directory> --arch x86_64 --arch
aarch64 --output <new-directory>` 使用公共 runner 的发现逻辑和共享外部镜像请求
函数。每个标签仅解析一次，保留精确 registry 响应供重试，使用 skopeo 下载，并
依据这些响应校验归档层和 config。守护进程缓存和凭据不进入镜像。已有采集文件
损坏时会失败，而非自动修复。

在每个原生架构运行 `build.py --version release-vX.Y.Z --cases
<assembled-case-directory> --deps-dir <collection> --output <stage>`，以已提交
输入构建，只保存一次规范镜像，并生成包含 image ID、归档哈希/大小、源码身份和
所有上下文文件的回执。固定的 Ubuntu 基础镜像以及官方 Go/Rust 归档校验值见
[Dockerfile](Dockerfile) 和 [toolchains.json](toolchains.json)；已安装系统包版本
保留在 `/usr/share/workbench/packages.tsv`。

运行 `python3 -B workbench/test_workbench.py` 验证单元契约。在合适的原生主机运行
`python3 -B workbench/test-system.py --image "$IMAGE" --root <new-task-path>`，
实际验证双实例隔离、守护进程故障、重启、超时、启动失败和名称冲突。添加
`--protect-container <existing-name>` 可记录既有服务未变化的身份和启动时间。结果
和保留输出位于该任务目录。这些检查补充原生完整构建和当前公共 E2E 用例，不能
替代无编译器的发布验收。

构建镜像之前，可运行 `python3 -B workbench/test-apparmor-mounts.py --root <new-task-path>`，
在私有命名空间验证检测掩蔽、具名网络命名空间、Docker 式 pivot/旧根传播、无关挂载
被拒绝以及所有权策略清理。它需要主机 `apparmor_parser`、`aa-exec`、`unshare`、`ip`
及 root 或 `sudo -n`；它不构成 Docker 或 KVM 验收。公共 x86 CI 在完整镜像测试前运行它。

在启用 enforcing AppArmor 的 Ubuntu 上，运行
`test-apparmor.py --image "$IMAGE" --root <new-task-path>`，检查外层与内层 PID
上下文、私有挂载/守护进程、无关挂载被拒绝、主机强制执行状态未变化以及所有权
清理。公共原生 x86 CI 要求此检查通过。该检查明确不验证 KVM/TUN 设备，因此可以
在无 KVM 的托管机器运行；它不能替代完整系统/KVM E2E。

附带的 [seccomp 基础策略](seccomp-default.json) 来自 Moby 默认策略
[65adc7e022c97f55e45c054ff012988027733b87](https://github.com/moby/profiles/blob/65adc7e022c97f55e45c054ff012988027733b87/seccomp/default.json)，
同时保留其 [Apache 2.0 许可证](LICENSE.seccomp)。`workbench` 为每个实例生成少量
受 capability/参数约束的附加规则，基础策略保持原样。上游发行版许可证随工具保留。
[外层 AppArmor 模板](apparmor.profile) 派生自
[Moby v26.1.3](https://github.com/moby/moby/blob/v26.1.3/profiles/apparmor/template.go)，
保留其 [Apache 2.0 许可证](LICENSE.apparmor)；嵌套运行所需的调整在此维护。
