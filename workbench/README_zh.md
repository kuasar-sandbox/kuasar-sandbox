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

系统模式是可信系统管理员工具，使用 rootful Docker 的 `--privileged`，并保留
私有命名空间及独立 daemon 数据。宿主只需原生 Linux、Docker Engine、Python 3、
具备所请求 cpu/memory/pids 控制器的 cgroup v2，以及适合镜像内 overlay2 的存储。
无需宿主 AppArmor 解析工具、策略加载器、自定义 seccomp 策略或另一套 Docker。
Docker 访问权限本身已具有管理员权限，不应将此环境暴露给不可信代码。

普通 UID 构建只需可写的任务源码及状态目录，不需要 KVM 或额外系统权限。
通用系统环境启动不再要求 KVM、4096 字节页、userfaultfd 或 BPF；启动时检查
systemd 与私有 daemon 就绪，并报告实际能力。所选产品测试仍须满足真实设备和
内核能力要求，不把不可用能力当成通过。启动器不管理宿主策略、全局 sysctl、
模块或已有服务。

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

这些套件是原生 ARM 的最小示例。托管 ARM 发布 CI 没有 KVM 设备，因此仅检查资产字节和镜像导入，不代表系统或离线验收。使用已发布镜像的独立原生 KVM 验收还在 ARM 执行 `sandbox.lifecycle.sh`、
`snapshot.restore.sh` 和 `network.tapfd.sh`；在两个命令都加入对应的三个 `--include`
选项可覆盖该范围。在 x86_64 上，prepare 和 run 都应使用当前完整
普通选择 `--all --exclude storage.obs.sh`。完整选择请使用 `--network bridge` 启动：prepare 的 `--offline` 仍禁止依赖下载，而 Demo 的 run 需要真实 Internet 出站访问。发布验收还会在 prepare 期间断开所属 bridge，仅在准备结束后重新连接。不得删除用例架构检查。需要凭据的 OBS
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
旧实例若记录了外部策略状态，必须先用原启动器完成清理再切换版本；新启动器
不修改这些旧策略。

每个系统实例仍有私有 PID/UTS/IPC/mount/network/cgroup 命名空间、bpffs、
Docker/containerd socket 与数据，以及只读发行输入。不使用宿主命名空间、
宿主 Docker socket 或宿主根目录绑定。系统模式通过 Docker privileged 获得
管理员能力和宿主设备访问权限；上述私有资源用于避免操作相互干扰，不是针对
恶意 root 的安全边界。外层容器和 daemon 的 nofile 上限为 1048576，满足内层
服务的实际需要。

入口只在私有命名空间中将宿主全局 sysfs/sysctl 视图设为只读，并屏蔽不适用于
容器的 udev 和模块加载服务。保留最小的私有绑定来遮蔽内层 Docker 的 AppArmor
探测输入，避免其加载宿主策略。不添加、替换或卸载宿主策略。这些措施用于避免
普通服务启动产生意外影响，不构成对恶意管理员的权限约束。

镜像为内部 systemd 单元设置 `DefaultTasksMax=infinity`。外层 `--pids-limit`
仍是整个环境的任务数上限，显式配置的单元级限制也继续生效，避免隐式的单服务
上限阻止嵌套服务启动。

仍保留 CPU quota、内存和进程预算，不再把所有实例自动绑定到宿主最前面的 N 个
CPU。空间检查不是磁盘配额；宿主内核、磁盘、网卡仍可能争用。仅用于可信管理员
任务，不用于不可信多租户，也不宣称性能完全隔离。

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

`test-system.py --smoke --image "$IMAGE" --root <new-task-path>` 使用两套
管理员实例验证真实内层容器、同名对象/端口、私有 daemon 和所有权清理。其微型
运行输入来自 workbench 内已有工具，不依赖下载测试镜像或 KVM。原生 CI 执行该
功能检查，不再执行宿主策略专项检查。完整系统 E2E、真实 KVM/UFFD/TUN/BPF
能力及无编译器验收仍独立保留，并按声明的产品范围要求通过。

安装工具保留其上游许可证及来源记录。原自定义策略源码及不再使用的 vendor
副本不再随包交付。
