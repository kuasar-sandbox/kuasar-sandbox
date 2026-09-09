[English](README.md) | [简体中文](README_zh.md)

# CI Runner slot

本目录在 openEuler 24.03 集成主机上,用独立的 `systemd-nspawn` 系统容器准备
四个候选 E2E slot 和两个 control slot。每个容器分别拥有 systemd、journald、
PID、mount、network、cgroup、Docker daemon、Runner 凭据和 Actions 工作目录。
容器只共享以下路径:

- `/var/cache/kuasar`:可写的源码/原生产物缓存。精确 SHA 名称和仓库锁用于协调
  复用,不能防止篡改;
- `/var/lib/kuasar-ci/tools`:只读测试工具;
- `/usr/local/go` 和主机内核模块树:只读。

每个 slot 在 `/sys/fs/bpf` 下绑定不同的 bpffs 子树,避免并行任务的 BPF pin 路径冲突。

主机必须预装 pin 的 x86_64 E2E 工具,不能在任务中下载这些大型发行资产:

| 路径 | SHA-256 |
| --- | --- |
| `/var/lib/kuasar-ci/tools/zot` | `523e5bf29a013db09115f780c3152af98fc5b65fc408a0d3e6c293643dc9bde7` |
| `/var/lib/kuasar-ci/tools/versitygw` | `e839f0ce24a51dbf0a7a925e08a28a0bfa190d05290c13f2c4536852bc5f3a7d` |

执行 `check` 前,从运维端传入已验证文件。provisioner 拒绝缺失或摘要不匹配的
工具,并把该目录只读挂载到全部 slot。

共享 Go 工具链固定为 `linux/amd64` 的 `go1.26.5`。若不存在已验证的
`/usr/local/go`,安装过程只从阿里云中国直连镜像 `https://mirrors.aliyun.com/golang/`
取得 `go1.26.5.linux-amd64.tar.gz`,核对 Go 发行 SHA-256
`5c2c3b16caefa1d968a94c1daca04a7ca301a496d9b086e17ad77bb81393f053`
后再替换工具链。任务本身不下载 Go distribution。

这些容器提供特权资源名称隔离,不是不可信任务的安全边界。它们有意获得 KVM、
TUN、vhost 设备、全部 capability、Docker keyring syscall 及 Connector 数据路径
需要的 `bpf` syscall。
候选代码不得获得发布权限、持久 Runner 凭据或其他任务可写的残留状态。这些容器
本身不满足该边界:root workload 共享特权主机,Runner 凭据也保留在 slot 内。
维护者接纳和只读 job token 不能消除这些风险。不可信 Fork 执行除了
[CI](../../docs/ci_zh.md) 的接纳检查,还需要经过验证的隔离边界。

## 主机布局

| Slot | 角色 | Runner 名称 | CPU/NUMA | 内存 | 地址 |
| --- | --- | --- | --- | --- | --- |
| 1 | E2E | `bms-tmp-kuasar-e2e-1` | NUMA0: `0-6,44-50` | high 52 GiB, max 56 GiB | `10.203.0.11/24` |
| 2 | E2E | `bms-tmp-kuasar-e2e-2` | NUMA0: `7-13,51-57` | high 52 GiB, max 56 GiB | `10.203.0.12/24` |
| 3 | control | `bms-tmp-kuasar-control-3` | NUMA0: `14-20,58-64` | high 52 GiB, max 56 GiB | `10.203.0.13/24` |
| 4 | E2E | `bms-tmp-kuasar-e2e-4` | NUMA1: `22-28,66-72` | high 52 GiB, max 56 GiB | `10.203.0.14/24` |
| 5 | control | `bms-tmp-kuasar-control-5` | NUMA1: `29-35,73-79` | high 52 GiB, max 56 GiB | `10.203.0.15/24` |
| 6 | E2E | `bms-tmp-kuasar-e2e-6` | NUMA1: `36-42,80-86` | high 52 GiB, max 56 GiB | `10.203.0.16/24` |

以上 Runner 名称是现有注册标识,不是一种验证方法的名称。修改文档名称不会重建注册。

布局在每个 NUMA node 保留一个物理核(`21,65` 和 `43,87`);所有 slot 达到
`MemoryMax` 时,主机仍保留约 39 GiB 内存。该配置面向功能 E2E 并发;需要原先
每 slot 176 GiB 布局的性能或密度基线应使用独立 profile。

主机 bridge 为 `kuasar-ci0`,地址 `10.203.0.1/24`。精确 iptables 规则通过当前
默认上联网卡对该子网做 NAT。网络服务在接口 alias 中记录所有权,拒绝修改或删除
同名但不属于它的接口。已有主机 Runner 存在活跃 `Runner.Worker` 时,provisioner
拒绝执行;同时要求 cgroup v2,并拒绝已启用且不属于所配置中国镜像的 DNF 源。
`check` 可在安装 `kmod` 前运行;`install` 先安装主机包,再加载所需模块并核对设备节点。

## 安装

在集成主机上以 root 从本目录执行:

```bash
./provision.sh check
./provision.sh install
```

安装根从已经配置的华为云 openEuler 镜像构建一次,再复制到
`/var/lib/machines/kuasar-ci-{1..6}`。openEuler 未打包 guest `mkfs.erofs` 所需的
静态 `libuuid.a`,因此 provisioner 在安装根内使用 pin 的 openEuler `util-linux`
source RPM 构建它。Source RPM 和其中的上游 tarball 都核对 SHA-256;
8 MiB RPM 从华为云下载并缓存到 `/var/cache/kuasar/sources`。
RocksDB 链接的 `cache-ctl` 所需 `libstdc++-static`,以及 Accelerator E2E 使用的
Redis server 也从同一镜像安装。GNU `time` 采集分阶段 CPU、内存和 I/O 指标。
每次安装都会对齐包清单,让已有 slot 取得新加入的构建依赖。

修改 util-linux pin 必须同时覆盖完整来源描述:
`KUASAR_UTIL_LINUX_SRPM_URL`、`KUASAR_UTIL_LINUX_SRPM_SHA256`、
`KUASAR_UTIL_LINUX_SOURCE_ARCHIVE` 和 `KUASAR_UTIL_LINUX_TARBALL_SHA256`。
归档名必须是普通文件名,并参与静态库构建身份计算。

模板把该次构建的许可正文与 `SOURCES.tsv` 保存在
`/usr/share/kuasar-ci/native-libuuid/<build-id>/`。记录将实际 `libuuid.a` 摘要与
通过校验的 source RPM、上游 tarball 绑定;`MATERIALS.sha256` 覆盖完整材料清单。
新建和已有 slot 都取得库及其对应目录。材料缺失或改变会使模板缓存失效;
slot 中的副本不匹配则准备失败。Runtime 打包消费该目录,因为本地构建的静态库
没有已安装 RPM owner。这些记录支持发行检视,不构成许可证合规认证。

主机安装还为 bridge、overlay、TUN 和 vhost 设备写入
`/etc/modules-load.d/kuasar-ci.conf`,使已启用 slot 在重启后仍有必需的绑定设备。
空间检查分别跟随模板与 `/var/lib/machines` 所在文件系统:两者共享文件系统时
需 35 GiB 可用空间;分开时分别需要 5 GiB 和 30 GiB。
provisioner 先在 staging root 写独立 owner marker,随后才暴露 slot root。
不完整但确属本次管理的 root 会在重试时重建;缺失或不匹配 marker 的 root 不会被
修改或删除。中断留下的所属 staging root 会在空间检查前清理,陌生 staging 路径
则被拒绝。完整的所属 root 始终原地对齐。所有修改性 provision 命令持有主机锁。

复制既有 Runner distribution 时不复制凭据、日志和庞大的工作目录。Runner
自动更新被禁用,避免容器意外下载大型国际发行资产;GitHub Runner 支持窗口要求
升级时,由运维更新主机 distribution 并明确重新安装。同步会删除新版 distribution
中已经移除的文件,只保留明确排除的凭据、diagnostics、工作目录、environment、
path 和注册 marker。安装前必须停止旧主机 Runner service;对齐包与 Runner 文件
期间,已有容器 slot 也必须停止。

Runner service 拒绝直接手动停止,且不限制启动重试速率,避免意外
`systemctl stop actions-runner` 或短时失败风暴让正常 slot 长期离线。计划维护
应使用 provisioner 的 `stop` 命令停止所属 `systemd-nspawn@kuasar-ci-N.service`。

每次 installroot 包操作前后,都会用主机仓库文件替换 openEuler release 包的
默认 metalink 配置,防止 `openEuler-release` 更新恢复国际 metalink。
主机 `/etc/yum.repos.d/*.repo` 应持续指向中国直连镜像,该配置也会传播到全部 slot。

生成短期组织注册 Token,通过运维人员已有的合法 SSH 连接流式传入。
provisioner 通过容器 stdin 把 Token 交给 Runner 的 `ACTIONS_RUNNER_INPUT_TOKEN`,
不会把它写入命令参数或文件:

```bash
CI_HOST=ci-host.example # 替换为既有集成主机的 SSH 别名.
gh api --method POST /orgs/kuasar-sandbox/actions/runners/registration-token \
  --jq .token | ssh "$CI_HOST" '/usr/local/sbin/kuasar-ci-runner-provision register 1'

gh api --method POST /orgs/kuasar-sandbox/actions/runners/registration-token \
  --jq .token | ssh "$CI_HOST" '/usr/local/sbin/kuasar-ci-runner-provision register 2'

# Slot 3 至 6 分别使用新 Token 重复执行.
```

只有 Runner 配置、固定 PATH 和 service enablement 全部成功后,才写入注册完成
marker。无 marker 的部分注册可通过新 Token 触发 Runner `--replace` 流程重试;
已经完成的注册保持不变。

E2E 与 control 角色不能通过原地换 label 切换。先 drain 并移除 GitHub Runner
注册,停止容器,更新 `CONTROL_SLOTS`,再从可信模板重建,最后使用新注册 Token:

```bash
ssh "$CI_HOST" '/usr/local/sbin/kuasar-ci-runner-provision rebuild 3'
```

`rebuild` 拒绝活跃或无归属的 slot。它先把旧 rootfs 移入私有隔离路径;
若新准备失败则恢复旧 rootfs,只有确认新 machine ID 和完整替代 root 后才删除
隔离副本。这会丢弃原 Actions 工作目录、Runner 凭据、diagnostics 和候选任务残留状态。

## GitHub 出站代理

中国大陆直连 GitHub 不稳定时,应在启动 slot 前配置组织控制的出站代理。
对 workload 而言该代理必须无凭据,通过网络侧策略授权访问。URL 留在仓库之外的
root 可读文件中,使用 Runner 消费的小写变量名:

```text
https_proxy=<organization-proxy-url>
http_proxy=<organization-proxy-url>
no_proxy=<internal-hosts-and-test-networks>
```

全部 slot 停止时,以 `0600` 权限把文件复制给每个已经注册的 Runner:

```bash
for slot in 1 2 3 4 5 6; do
  install -m 0600 /etc/kuasar-ci/runner-proxy.env \
    "/var/lib/machines/kuasar-ci-$slot/opt/actions-runner/.env"
done
```

Runner 只在启动时读取 `.env`,因此每次改代理后都要重启 slot。provisioner
对齐 Runner distribution 时有意保留各 Runner 的 `.env`。已接纳的 Fork 候选
仍须满足上述隔离要求;Runner 代理必须使用网络侧访问控制,不能把可复用凭据
放在 workload 能读取的位置。

`.env` 的 `no_proxy` 必须包含本地控制端点、沙箱测试网络和内部 Registry,
让 E2E 流量留在本地。工作流不覆盖这些变量;没有代理时也保留 Runner 原有直连环境。

Slot 1、2、4、6 加入组织 `kuasar-e2e` group,带有
`kuasar-e2e,kvm,cgroup-v2` 及各自 slot label。该 group 为组织范围
`visibility=all`,当前没有 workflow allowlist。这些分配设置和只读 job token
都不是不可信代码安全边界。不能把 group 可见性当作特权代码执行的接纳条件。

Slot 3、5 加入 `kuasar-control`,只带 `kuasar-control` 和 control-slot label,
并拥有独立 rootfs 与工作目录。Control group 使用组织范围可见性并允许公开组织
仓库,因此新增或公开仓库不需要更新 group 成员。精确 workflow allowlist 仍将
分配限制为可信中央 `ci-entry.yml` 及组件 `main` 上的发行工作流。
CI 接纳/最终校验和发行控制 job 可以持有写凭据,但绝不能执行候选源码。
候选 E2E job 不得选择 control group,control job 也不得选择 E2E slot。
迁移入口时,先添加并验证新的精确 workflow 路径,再清理旧 allowlist 或必需检查。
新的来源绑定检查实际成功执行之前,必须保留旧门禁。

容器共享物理主机,有意采用特权资源隔离,不能防御恶意的主机级逃逸。
应把集成主机作为可信基础设施运维,持续维护 control Runner 的精确 workflow
allowlist;slot 改角色时必须重建,不能仅换 label。

启动并检查基础设施资源分离:

```bash
ssh "$CI_HOST" '/usr/local/sbin/kuasar-ci-runner-provision start'
ssh "$CI_HOST" '/usr/local/sbin/kuasar-ci-runner-provision verify'
```

`start` 会再次确认保留的旧主机 Runner 已停止。若某个容器启动或就绪失败,
它会停止该命令已经启动的每个 slot,恢复各 unit 原先的 enabled 状态;
命令执行前就已活跃的 slot 保持活跃。

回滚时停止全部容器 slot,不删除状态。停止某个容器失败会让命令失败,
不会把仍在运行的 slot 当作已经停止:

```bash
ssh "$CI_HOST" '/usr/local/sbin/kuasar-ci-runner-provision stop'
```

`start` 和 `verify` 要求当前 Runner service invocation 报告
`Listening for Jobs`,不能匹配同次主机启动中更早进程的日志,也不能把持续重试
但 active 的 service 当作在线。`verify` 还检查 PID1/systemd、cgroup v2、
KVM/TUN/vhost 访问、独立 mount/network namespace、不同主机 cgroup、嵌套 Docker、
独立 bpffs/netns、静态 `libuuid` 构建依赖、通过中国侧代理路径的出站访问和
活跃 Runner service。它不是 E2E wrapper;仓库工作流仍直接执行 `make test-e2e`。

所有 slot 必须三次并行通过完整组装的 Integration E2E 门禁,并验证取消清理后,
才可停止或注销旧主机 Runner。迁移期间可以停止但保留它,作为快速回滚路径。
