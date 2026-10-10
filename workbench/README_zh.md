[English](README.md) | [简体中文](README_zh.md)

# Workbench

运行第一个沙箱请先按[快速开始](../docs/quickstart_zh.md)操作；
[获取发布版](../docs/download_zh.md)提供无需源码检出的制品校验和镜像导入步骤。
本文维护环境与生命周期合同，不重复首次体验步骤。发布版示例从解包目录执行，
源码构建示例从主仓目录执行，启动器均使用与输入匹配的版本。

Workbench 为选定的 Kuasar Sandbox 发布版提供原生 Linux 构建工具和系统环境。
同一个镜像支持普通 UID 构建，以及以 systemd 为 PID 1、包含私有 Docker/containerd
的环境。镜像包含外部测试镜像归档；产品、测试辅助程序和 Demo wheel 则来自匹配的
发布版。选择测试请参阅[公共 E2E 指南](../test/README_zh.md)，完整示例请参阅
[Demo 指南](../test/demo/DEMO_zh.md)。

## 主机与镜像

使用原生 x86_64 或 aarch64 Linux、Docker Engine 和 Python 3.9+。启动器使用本机的
Unix Docker 端点。Docker Desktop、远程 Docker 端点、架构模拟、Podman 和 runner
注册不属于 V1 范围。

系统模式是可信系统管理员工具，使用 rootful Docker 的 `--privileged`，并保留
私有命名空间及独立 daemon 数据。宿主只需原生 Linux、Docker Engine、Python 3.9+、
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

### Windows 与 WSL 2 开发宿主

只有 Linux 环境满足所选发布版的原生要求时，WSL 才可用于开发。发行版能启动或存在
`/dev/kvm` 均不足以证明可用；Windows、CPU、固件和可能存在的外层 hypervisor 必须
实际暴露嵌套虚拟化能力。本流程不为所有 WSL 组合或 ARM KVM 执行提供统一认证。
Workbench 无法补齐缺失的宿主能力。

先在本机 PowerShell 确认 Windows 机器和目标发行版，不要配置无关的远程 Linux shell：

```powershell
hostname
whoami
Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model,HypervisorPresent,TotalPhysicalMemory
Get-CimInstance Win32_Processor | Select-Object Name,NumberOfLogicalProcessors,VirtualizationFirmwareEnabled,SecondLevelAddressTranslationExtensions
wsl --version
wsl --list --verbose
```

变更前记录 Windows build、磁盘剩余空间、已有 WSL/Docker 工作负载及配置。结合正在
运行的 hypervisor 和 Linux 内实际 KVM 探测判断固件/SLAT 字段；hypervisor 已运行时
可能屏蔽硬件字段。若 Windows 本身是 VM，须由外层宿主的管理者开放嵌套能力。VM
处理器配置适用于该 VM，不适用于普通物理 Windows 宿主。

使用 WSL 2 发行版内的本机 rootful Linux Docker Engine 及其 Unix socket。检查当前
Docker context/endpoint、daemon 内核/架构、存储驱动、数据目录和真实容器网络；选择
端点时保留已有 Docker 数据。发行版内记录 `uname -r`、`uname -m`、`getconf PAGESIZE`、
`nproc`、`free -h`、`df -h`、PID 1、`/sys/fs/cgroup/cgroup.controllers` 和实际 cgroup
预算。若发行版 daemon 由 systemd 管理，按
[Microsoft 发行版配置](https://learn.microsoft.com/en-us/windows/wsl/systemd)启用 systemd，
验证 PID 1 和 `systemctl status docker`；Workbench 内的 systemd 是另一个实例。
systemd 服务本身不会保持 WSL 存活。长时间构建和测试应保留日志、进程退出状态及
可恢复的阶段记录。

若已安装内核缺少所需功能，应从与本机 WSL 版本兼容的 Microsoft tag/commit 构建
**WSL 宿主内核**并记录版本。以该版本的 Microsoft WSL 配置为基础，保留 Hyper-V、
存储、网络和互操作支持。遵循该检出的
[官方构建与 VHDX 说明](https://github.com/microsoft/WSL2-Linux-Kernel#build-instructions)：
不同内核版本的打包脚本和布局可能不同。构建匹配模块及模块/artifacts VHDX，记录
kernel release、配置差异和产物哈希。不得用 Kuasar Guest defconfig 替代，也不应为
配置开发宿主而修改 Guest ABI 或项目最低内核基线。

按所选测试核对宿主功能：KVM 及 CPU 厂商模块；namespaces、cgroup v2 控制器/委托、
seccomp 和 overlayfs；userfaultfd、memfd 和 shmem；BPF/JIT/BTF、bpffs 和 TC BPF；
TUN/TAP、veth、bridge、GENEVE、conntrack 和 NAT；以及实际需要的 vsock/vhost/存储
路径。NFS、FUSE、EROFS 按宿主实际用途核对；安装用户态工具不等于启用内核功能。
核验模块与 `uname -r` 匹配并可加载。在原生 Linux 宿主可用以下无持久修改的探测
打开并关闭一个空 KVM VM：

```sh
sudo python3 - <<'PY'
import fcntl, os
with open('/dev/kvm', 'rb+', buffering=0) as kvm:
    assert fcntl.ioctl(kvm, 0xAE00, 0) == 12, 'unexpected KVM API version'
    vm = fcntl.ioctl(kvm, 0xAE01, 0)
    os.close(vm)
PY
```

探测通过后仍须运行真实 Kuasar Guest。同样，应通过所选源码/产品检查验证实际
userfaultfd 操作、BPF 加载、cgroup 委托及 TAP/NAT 通信；配置符号或设备列表不能
替代功能验收。

将原 `%UserProfile%\.wslconfig` 和内核/模块产物保存在 Windows 可访问的恢复位置。
按 [Microsoft 配置参考](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)合并
选定的 `kernel`、`kernelModules`、`nestedVirtualization`、`processors`、`memory`、
`swap`，保留无关设置。这些选项影响该用户的 WSL 2 VM；执行 `wsl --shutdown` 前，
应为所有正在运行的发行版协调中断窗口。重启后核验实际内核、模块及预算。若无法
启动，从 PowerShell 恢复配置备份并重启 WSL；若之前没有配置，仅移除本次新增的
覆盖项。回退不能依赖可用的 Linux shell。

### 资源预算与验收

Windows、WSL 内核/服务及外层 Docker 的预算与 Workbench 限额分开计算，每层均保留
CPU 和内存余量；swap 不能替代活动 VM 所需 RAM。Workbench 示例既不是 WSL 总预算，
也不是完整测试的最低配置。源码/构建树、缓存及容器数据优先放在发行版原生 Linux
文件系统中。估算内核产物、镜像归档/层、准备输入、重复用例及大镜像测试空间，并
同时检查 Linux 文件系统和承载 VHDX 的 Windows 卷剩余空间；虚拟磁盘最大容量不等于
可用存储。

共享 Workbench 环境时，为 node 显式分配预算。
[节点预留模型](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-resource_zh.md)
规定 `physical_memory: auto` 读取宿主 `MemTotal`，并非容器内存限额。启动池是准入预算，
不是预先分配的 RAM：

```text
AllocatablePool = (Physical - HostReserved) * (1 - operational_margin_factor)
StartupPool     = AllocatablePool * startup_factor
```

实现使用饱和减法及字节取整。例如分配 10 GiB、保留 1 GiB、margin 为 0.10、startup
factor 为 0.50，得到约 8.1 GiB 可分配池和 4.05 GiB 启动准入池。6 GiB 启动预留请求
需要足够的池及有效策略；增大因子不会创造物理容量。保留节点文档中的参数边界，
提高并发前测量真实内存峰值和 OOM 事件。Guest capacity、当前 allocation 和 startup
reservation 是不同数量。

运行匹配版本的 [Demo](../docs/quickstart_zh.md)，验证真实创建、exec、文件操作、
DNS/出网、快照/恢复及清理，再在相同原生架构执行
[普通全量选择](../test/QUICKSTART_zh.md)。保留版本、用例选择、退出码、日志及资源
峰值。缺少必需能力即验收失败，不能缩减工作负载断言以宣称支持更小机器。OBS 需要
额外凭证，不属于普通全量。DNS/出网失败时，先定位 Windows、WSL、外层容器或 Guest
中的故障边界，再检查该实例的 DNS、代理、路由和 MTU。不要规定通用 DNS 地址/MSS、
无依据切换防火墙后端或关闭宿主防火墙。

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

以上示例只是子集，不代表完整覆盖。在原生 x86_64 和 aarch64 上，prepare 和 run
都使用 `--all --exclude storage.obs.sh`，从选定发布版生成完整常规用例集合。
ARM 全量执行要求真实 KVM/内核前置条件、支持 ARM PMEM/DAX 的 Guest 内核，
以及发布包中的静态 ARM `cgroup-fork-probe`；必要输入缺失时在 prepare 阶段失败。
用例数量随发布版文件变化，不固定为某次历史数量。

全量选择使用 `--network bridge` 启动：prepare 的 `--offline` 禁止依赖下载，
Demo 的 run 则要求真实 Internet 出站。发布验收还会在 prepare 期间断开所属
bridge，准备完成后重新连接。保留全部架构、DAX 和退出码断言。
需要凭据的 OBS 仍须显式选择，并提供相应网络和凭据。

新的原生发布验收记录 `qualification_scope=native-full`，绑定完整用例列表、
退出码、内核/helper/产品来源及 workbench 身份。历史 `system` 证据保留原有
选择范围，不能改标为全量。没有 KVM 的托管 ARM 保留非 KVM CI 和
`artifact-only` 发布验收，不声称完成系统或离线执行。

`--network none` 移除外层实例的外部网络，私有 Guest、Registry、Store 和 Proxy
通信仍然可用。镜像默认设置 `E2E_DEPS_DIR=/opt/workbench/deps` 和 `E2E_OFFLINE=1`。
若有意在以 `--network bridge` 启动的实例中在线准备，请使用
`exec -- env E2E_OFFLINE=0 python3 ...`，不要加 `--offline`。只传递命令所需的任务
变量。离线模式控制依赖获取，不选择用例。缺失或损坏的必要输入会失败；辅助程序和
锁定 wheel 始终来自选定的 platform-release，不回退到 PATH 或全局 Python 包。

完整 Demo 和常规全量测试应新建 bridge 网络实例，不能只把上面 none 网络示例的
suite 改成 `--all`。按所选负载增加 CPU/内存/磁盘余量；修改预算或挂载需要新实例。
两个终端访问同一 Demo 时，都应通过同一个 `--root`/`--name` 的 `exec` 进入。

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
