# Platform release 测试指南

本文件位于 `platform-release-vX.Y.Z.tar.gz` 的 `test/QUICKSTART.md`。platform 包集中交付
系统及组件文档、组件 E2E、platform 组合用例、性能脚本和 demo;六个组件包只交付运行制品。
把同一个聚合 Release 的七个 archive 解压到同一目录后即可执行完整验证。

## 1. 解包布局

```text
<release-dir>/
├── bin/                         六个组件包提供的运行制品
├── deploy/                      组件部署文件
├── docs/                        platform 与五个组件的聚合文档
└── test/
    ├── QUICKSTART.md
    ├── e2e/
    │   ├── run_all.sh           完整发布门禁
    │   ├── accelerator/
    │   ├── connector/
    │   ├── guest-runtime/
    │   ├── sandboxer/
    │   ├── orchestrator/
    │   └── platform/            真正跨组件组合本身的用例
    ├── perf/
    └── demo/
```

各 owner 目录只有一个稳定入口 `run_all.sh`。组件特性用例始终保存在对应组件目录,即使它
依赖其他仓的二进制、KVM 或 platform 提供的服务环境。platform 目录只保存无法归属于单一
组件的组合用例。

## 2. 解压与校验

下载同一聚合 Release 的全部显式资产后先校验:

```bash
sha256sum --quiet -c SHA256SUMS
mkdir kuasar-sandbox-release
for archive in *.tar.gz; do
    tar -xzf "$archive" -C kuasar-sandbox-release
done
cd kuasar-sandbox-release
```

archive 已由发布流程检查路径安全和跨包覆盖。不要混用不同聚合 Release 下载的
`SHA256SUMS`、platform 包和组件包。

## 3. 前置条件

完整门禁需要:

- Linux x86_64、systemd、cgroup v2 与可读写 `/dev/kvm`;
- root 或无交互 `sudo`;
- Docker、iproute2、curl、Python 3、openssl、mkfs.ext4;
- 可执行的本地 OCI registry `zot`;
- 可执行的 S3-compatible 测试网关 `versitygw`;
- 能拉取用例使用的基础镜像,或预先准备对应镜像。

`bin/` 默认从解压根目录自动定位,也可用 `BIN=/path/to/bin` 覆盖。OBS 用例是唯一的凭据型
可选套件;只有设置 `OBS_E2E=1` 才运行。

## 4. 运行完整门禁

```bash
ZOT_BIN=/path/to/zot \
VGW_BIN=/path/to/versitygw \
bash test/e2e/run_all.sh
```

顶层入口依次执行 accelerator、connector、guest-runtime、sandboxer、orchestrator 和
platform 的 `run_all.sh`。任一 owner 失败即停止,成功结尾为:

```text
==> full release e2e: OK
```

## 5. 运行组件或单项用例

只运行一个 owner:

```bash
BIN=$PWD/bin bash test/e2e/accelerator/run_all.sh
BIN=$PWD/bin bash test/e2e/sandboxer/run_all.sh
BIN=$PWD/bin ZOT_BIN=/path/to/zot VGW_BIN=/path/to/versitygw \
    bash test/e2e/orchestrator/run_all.sh
```

直接运行单项时,按脚本头部说明设置环境:

```bash
BIN=$PWD/bin bash test/e2e/sandboxer/e2e_sandbox_cold.sh
BIN=$PWD/bin ZOT_BIN=/path/to/zot \
    bash test/e2e/orchestrator/e2e_run_builder.sh
```

主要归属为:

- accelerator:manifest、cache、store、OBS;
- connector:eBPF/TC 网络拓扑与 tap;
- guest-runtime:OCI 展平与 runtime bundle;
- sandboxer:冷启动、磁盘、快照、恢复、stdio、tapfd;
- orchestrator:node/proxy、builder、exec、MMDS、cluster、resource density;
- platform:warm-pool 跨 sandboxer/accelerator 去重组合。

## 6. Perf 与 demo

性能入口位于 `test/perf/`,e2b SDK 演示位于 `test/demo/`。它们与 E2E 共享同一个 `bin/`
布局,但不属于组件 PR 的 `run_all.sh` correctness 门禁。常用入口:

```bash
BIN=$PWD/bin bash test/perf/sandbox-perf.sh
bash test/demo/demo_prep.sh
sudo bash test/demo/demo_e2b.sh
```

## 7. 排错

- `missing executable .../run_all.sh`:platform 包与组件选择不完整或混用了不同版本;
- `missing ... in BIN`:没有解压全部六个组件 archive;
- `/dev/kvm` 不可用:检查设备权限与 runner 虚拟化配置;
- `sudo -n` 失败:为测试 runner 配置所需的无交互权限;
- 镜像拉取失败:预拉取脚本指定的镜像或配置可用镜像代理;
- 需要保留现场:按具体脚本支持设置 `E2E_KEEP=1`。
