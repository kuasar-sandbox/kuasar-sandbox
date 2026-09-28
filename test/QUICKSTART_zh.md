[English](QUICKSTART.md) | [简体中文](QUICKSTART_zh.md)

# 平台发布验证指南

本指南覆盖聚合产品 E2E 与发布验收。首次安装请使用[快速开始](../docs/quickstart_zh.md)。

platform 包包含文档、公共 runner、扁平用例、预构建测试 helper、性能脚本和 Demo；六个组件包包含运行制品。使用同一聚合 Release 的 platform 包及目标架构的六个组件包。

<a id="1-解包布局"></a>
## 1. 解包布局

```text
<release-dir>/
├── bin/                         单架构预构建产品
├── deploy/
├── docs/
└── test/
    ├── QUICKSTART.md
    ├── e2e/
    │   ├── e2e                  公共 list / prepare / run 入口
    │   ├── cases/               <suite>.<case>.sh
    │   ├── lib/                 按 owner 命名空间组织的底层 helper
    │   └── helpers/<arch>/      预构建程序及源码/摘要清单
    ├── perf/
    └── demo/
```

完整文件名是用例 ID。第一段属于九个 suite 之一：`basic`、`storage`、`image`、`network`、`sandbox`、`snapshot`、`orchestrator`、`builder`、`telemetry`。没有 owner runner 或第二条产品执行路径。

<a id="2-解压与校验"></a>
## 2. 校验与解压

按 Release 校验和验证下载资产。只将目标架构的六个组件包与匹配 platform 包解压到同一全新目录。不要把两个架构覆盖到同一 `bin/`，不要混用不同聚合 Release。

```bash
sha256sum --quiet -c SHA256SUMS
mkdir kuasar-sandbox-release
# 将名称替换为明确选定的 Release 资产。
tar -xzf platform-release-vX.Y.Z.tar.gz -C kuasar-sandbox-release
# 再将选定的六个组件归档解压到同一目录。
```

打包验证路径、跨包冲突、helper 架构、摘要及独立测试源码 pin。产品与测试可以来自不同 revision，其身份分别记录。

<a id="3-前置条件"></a>
## 3. 前置条件

完整原生 x86 执行要求 Linux、systemd、cgroup v2、可用 `/dev/kvm`、root 或非交互 `sudo`，以及 Docker、iproute2、curl、Python 3.11+、openssl、EROFS reader、mkfs.ext4 和已选脚本要求的普通工具。prepare 需要访问声明的镜像和固定 Python wheel。registry/gateway/probe 程序来自包内。

prepare 和产品执行不需要 Go/Rust 编译器或组件源码 checkout。缺少预备输入时 runner 失败，不使用宿主机 helper，不在执行时拉取镜像。预备输入必须保持不变；可变用例状态和结果文件位于预备目录之外。

<a id="4-运行完整门禁"></a>
## 4. 准备并执行

```bash
release_dir="$PWD/kuasar-sandbox-release"
prepared="/var/tmp/kuasar-prepared"
python3 "$release_dir/test/e2e/e2e" prepare --release-dir "$release_dir" \
    --workdir "$prepared" --arch x86_64 --all --exclude storage.obs.sh
sudo python3 "$prepared/test/e2e/e2e" run --workdir "$prepared" \
    --all --exclude storage.obs.sh --result /var/tmp/kuasar-e2e-result.json
```

使用全新 prepare 目录。prepare 获取不可变镜像归档与 Demo SDK，记录摘要和权限，不编译产品或 helper。执行加载这些镜像归档，逐个调用 Bash 用例，记录完整文件名、耗时、退出状态，再验证输入树。被测 Build、flatten、snapshot 和发布操作仍真实执行。

任何非零退出、缺少前置条件或输入变化都会失败。`PASS <filename>` 和 `result.json` 只代表实际执行的用例；Draft 跳过与静态架构检查不算产品验收。`storage.obs.sh` 需要显式选择及其文档声明的外部存储凭据，普通公共验证保持排除。

<a id="5-运行组件或单项用例"></a>
## 5. 选择 suite 或单个用例

```bash
python3 "$release_dir/test/e2e/e2e" list --suite storage
python3 "$release_dir/test/e2e/e2e" list --suite snapshot --include image.flatten.sh
python3 "$release_dir/test/e2e/e2e" prepare --release-dir "$release_dir" \
    --workdir /var/tmp/storage-prepared --suite storage --exclude storage.obs.sh
sudo python3 /var/tmp/storage-prepared/test/e2e/e2e run \
    --workdir /var/tmp/storage-prepared --suite storage --exclude storage.obs.sh
```

重复的 `--suite` 与 `--include` 取并集，再按文件名排除。未知 suite、未知文件名或空结果都会失败；运行未 prepare 的用例也会失败。一个完整 suite 可以跨多个组件 owner，源码归属见[测试组织](README_zh.md)。

ARM CI 选择 accelerator 的 storage/image 及 guest-runtime 的 flatten/registry 用例，排除需要凭据的 OBS。其他用例明确记录为该 lane 的排除项；ARM 构建或 static lane 不证明这些用例可运行。源码 unit/race/vet、helper、UFFD 和 working-set 门禁独立于九个产品 suite。

<a id="6-perf-与-demo"></a>
## 6. 性能与 Demo

`basic.demo.sh` 用预备产品、镜像和 SDK 运行文档中的 Demo，保留真实 COPY/Build、Quick Start、exec/files、fan-out、迁移、持久准备与清理断言。独立用户 Demo 用法仍见 [Demo 指南](demo/DEMO_zh.md)。

性能 harness 留在 `test/perf/`。warm-pool 表征入口为 `test/perf/warmpool-dedup.sh`，名称不代表一般性的跨 VM snapshot 去重保证。UFFD 性能和 working-set smoke 保持独立源码门禁。smoke 结果不是统计性能验收；需引用精确 revision、job 日志和原始测量。

<a id="7-排错"></a>
## 7. 排错与验收

- 缺少产品/helper：使用完整匹配的发行输入；执行没有源码或宿主机回退。
- 摘要或权限变化：从已验证发行输入创建全新预备目录。
- 缺少 `/dev/kvm`、systemd 或权限：在满足所选用例前置条件的主机执行。
- 镜像或 SDK 获取失败：修复 prepare 的访问条件后使用全新目录；执行不会拉取或安装替代项。
- 用例失败：检查输出与记录的退出码。恢复诊断只保留有界错误词汇，不复制原始 capability。

验收要求精确 head/base 和测试/产品/helper 来源身份、必需源码门禁成功及真实原生产品结果。干净发行验收还需证明无组件源码树和 Go/Rust 工具链，覆盖一个完整非 KVM suite、一个完整 KVM suite 及适用 ARM 非 KVM 选择。保留失败和跳过结果；本地 fixture 测试不能替代公共 runner 验收。
