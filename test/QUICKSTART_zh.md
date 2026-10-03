[English](QUICKSTART.md) | [简体中文](QUICKSTART_zh.md)

# 平台发布验证

本指南验证预构建产品用例。首次运行沙箱见[快速开始](../docs/quickstart_zh.md)；
短 Demo 不代表完整发布验收。公共 runner 是唯一产品 E2E 执行路径。Workbench
提供环境，不是另一套 runner、产品包或用例选择器。

<a id="1-解包布局"></a>
## 1. 发布输入与目录

通过[获取匹配的发布版](../docs/download_zh.md)取得一个具体聚合版本的本机架构产品、
platform 材料及匹配 workbench 镜像。即使不下载另一架构，该流程也逐一验证所选
必需文件。不要用要求所有未选文件存在的全量校验命令，也不能忽略缺失的必需文件。
Workbench 的 Docker 归档应单独导入，不能解包到此树中。

```text
<release-dir>/
├── bin/                         Native products from component archives
├── guide/                       Selected bilingual user guides
├── workbench/                   Host launcher and its guide
└── test/
    ├── QUICKSTART.md
    ├── e2e/
    │   ├── e2e                  Public list / prepare / run
    │   ├── cases/               <suite>.<case>.sh
    │   ├── lib/                 Owner-namespaced runtime helpers
    │   └── helpers/<arch>/      Prebuilt programs and identity records
    └── demo/                    Demo scripts, prepared adapter and locked wheels
```

Platform 包包含 `guide/`、公共测试/运行输入及轻量 workbench 启动器。组件归档还
可能增加自身的 `deploy/`、`share/` 和许可材料。新的 platform 包不包含源码单测、
组装工具或 `test/perf/` harness。历史版本可能使用 `docs/`，应按其配套指南执行，
不要假定具备新布局。

完整文件名是用例 ID，第一段属于 `basic`、`storage`、`image`、`network`、`sandbox`、
`snapshot`、`orchestrator`、`builder`、`telemetry` 九个 suite。Owner 决定源码维护
归属，不产生另一套 runner 或选择方式。

<a id="2-解压与校验"></a>
<a id="3-前置条件"></a>
## 2. 环境与实际覆盖范围

推荐使用原生 workbench system 模式，宿主前置和模式边界见
[Workbench](../workbench/README_zh.md)。它提供 systemd、Docker、Python 3.12、EROFS
reader 和普通工具；所选用例仍需真实 KVM、内核、网络和设备能力。通用启动成功
不等于产品通过。保留资源余量，示例 CPU/内存预算只是起点，不是通用完整测试容量保证。

原生 x86_64 和 aarch64 使用同一个当前常规选择：`--all --exclude storage.obs.sh`。
ARM KVM 用例需要所选发布版中正确的 Guest PMEM/DAX 内核和原生静态 cgroup probe。
缺少必需输入时 prepare 失败，不进行替换。没有 KVM 的托管 ARM 仍保留明确较小的
非 KVM/artifact-only 范围，历史子集结果不能改称 native-full。

准备和执行消费产品、精确 helper 包及本地锁定 Demo wheel，不现场编译，也不发现
兄弟源码检出。Prepare 从实际 runtime bundle 读取 `/sbin/init` 来确定 telemetry
身份，不用独立 init 程序替代。可变运行状态和结果位于封存输入之外。

<a id="4-运行完整门禁"></a>
## 3. 在 workbench 内准备并执行

在**宿主机**保留获取阶段的 `RELEASE`、`IMAGE` 和 `ARCH`；`exec --` 之后的内容
在 **workbench 内**执行：

```bash
STATE="$PWD/kuasar-validation-state"
NAME="verify-$(date +%s)"
WB="$RELEASE/workbench/workbench"
python3 "$WB" --root "$STATE" --name "$NAME" start \
  --image "$IMAGE" --mode system --inputs "$RELEASE" \
  --cpus 8 --memory-gib 16 --network bridge
python3 "$WB" --root "$STATE" --name "$NAME" exec -- \
  python3 -B /inputs/release/test/e2e/e2e prepare \
  --release-dir /inputs/release --workdir /work/prepared --arch "$ARCH" \
  --all --exclude storage.obs.sh --deps-dir /opt/workbench/deps --offline
python3 "$WB" --root "$STATE" --name "$NAME" exec -- \
  python3 -B /work/prepared/test/e2e/e2e run --workdir /work/prepared --arch "$ARCH" \
  --all --exclude storage.obs.sh --run-root /work/run-1 \
  --out-root /output/cases --result /output/result.json
```

Offline prepare 表示不下载依赖；完整执行包含 Demo Internet 出站，因此需要 bridge
网络。本地 Guest/Registry/Store/Proxy 通信仍允许。需要凭据的 OBS 须显式选择并
提供其文档要求的凭据和网络，不会静默跳过，也不纳入普通公共验证。

使用全新的预备目录，只有完整输入才会封存。复用不可变预备集合时，选择新的
`--run-root` 和结果/输出路径。`PASS <filename>` 与 `result.json` 只代表实际运行的
用例。非零退出、缺少前置或输入变化均失败。Runner 记录文件名、耗时和退出码；
应查看控制台与用例文件，而不只看最终汇总状态。

### 原生替代路径

同一个 runner 可以直接在原生 systemd/cgroup-v2 主机上执行，需要 root/非交互 sudo、
真实设备、Docker、Python 3.12、EROFS reader（`fsck.erofs --extract`、`dump.erofs --cat`）
及所选用例的普通工具/库。将 `RELEASE` 设置为已验证的解包目录，`ARCH` 设置为本机
架构；此路径不依赖 workbench：

```bash
release_dir="$RELEASE"
prepared="$PWD/kuasar-prepared"
python3 -B "$release_dir/test/e2e/e2e" prepare --release-dir "$release_dir" \
  --workdir "$prepared" --arch "$ARCH" --all --exclude storage.obs.sh
sudo -n python3 -B "$prepared/test/e2e/e2e" run --workdir "$prepared" \
  --arch "$ARCH" --all --exclude storage.obs.sh --result "$PWD/kuasar-e2e-result.json"
```

下面的本地输入合同适用于 workbench 和原生主机。原生示例中的 `release_dir`
指向已解包输入；workbench 已默认使用验证过的 `/opt/workbench/deps` 和 `E2E_OFFLINE=1`。
### 本地及离线准备

获取并验证发行制品和依赖输入后，指定本地镜像目录：

```bash
python3 "$release_dir/test/e2e/e2e" prepare --release-dir "$release_dir" \
    --workdir /var/tmp/offline-prepared --arch "$ARCH" --all --exclude storage.obs.sh \
    --deps-dir /inputs/deps --offline
```

`E2E_DEPS_DIR` 是目录别名，`--deps-dir` 优先。`E2E_OFFLINE` 仅接受 `0` 或 `1`。`--offline` 始终禁止下载依赖，即使 `E2E_OFFLINE=0`。未设置选项或环境配置时，prepare 保持默认在线行为。未指定 `--offline` 时，`E2E_OFFLINE=0` 允许为缺失输入回退远端。显式为空、不存在或为符号链接的目录会失败。使用 `sudo` 时，在权限转换后显式传递这些选项，不保留整个用户环境。

目录包含扁平的 `images.json` 列表及 Docker 镜像归档。每条记录有 `reference`、`platform`（`linux/amd64` 或 `linux/arm64`）、`image_id`（config SHA-256）、相对路径 `archive` 和 `sha256`（归档字节摘要）。Registry 证据包括 `manifest`（精确原始 JSON 文本）、`manifest_digest` 和 `registry_digest`；index 响应还包含精确原始 `index` 文本及 `index_digest`。这些身份互不等同。manifest 的摘要必须与记录一致，并绑定归档中实际的 config；归档中每一层必须符合 config 中对应的未压缩层摘要。index 必须绑定唯一匹配平台的 manifest。`@sha256:...` 请求必须符合已验证 registry 响应摘要。仅自行声明摘要字段会被拒绝。移动 tag 直接使用记录的解析结果，不向远端检查新鲜度；应从已验证的发行输入取得目录，因为离线内容检查无法认证任意作者提供的 tag 映射。

prepare 精确匹配请求的 reference 与 platform。有效的已选归档直接复制，不重复加载/保存；只有本地派生 orchestrator fixture 时，prepare 才需要将 Python 基础镜像加载到 Docker。离线缺少已选输入时，错误包含 reference、platform 和查找位置。在线模式可下载缺失输入，但匹配记录损坏、不安全、有歧义或身份错误时始终失败，不从远端修复。归档与描述路径不得越出目录或使用符号链接；不安全的归档路径、重复成员、硬链接和设备会被拒绝。归档中只允许 Skopeo 的旧版 `<hex>/layer.tar` 符号链接别名指向 `../<diff-id>.tar`：目标必须是 manifest 选择的普通层文件，且其实际字节必须匹配 config 摘要。验证时不会跟随这些别名；选中的 config 和层必须仍为普通文件。未选择的归档不是前置条件。完成时将镜像证据及文件摘要/权限记录到 `provenance.json`；失败的 prepare 不会创建请求的工作区。

offline 控制依赖获取，不改变用例选择，也不禁止本地 Guest/Registry/Store/Proxy 流量。helper 仍来自精确匹配的包，Demo SDK 仍仅从本地 hash-locked wheelhouse 安装。需要凭据的 OBS 不会静默跳过。CI 的 `prepare-artifacts.py` 将相同选项传给公共 runner，并在 clean prepare 中将配置的依赖目录以只读方式挂载，与可写输出分离。


<a id="5-运行组件或单项用例"></a>
## 4. 选择 suite 或单个用例

```bash
python3 "$WB" --root "$STATE" --name "$NAME" exec -- \
  python3 -B /inputs/release/test/e2e/e2e list --suite storage
python3 "$WB" --root "$STATE" --name "$NAME" exec -- \
  python3 -B /inputs/release/test/e2e/e2e prepare --release-dir /inputs/release \
  --workdir /work/storage-prepared --arch "$ARCH" \
  --suite storage --exclude storage.obs.sh --offline
python3 "$WB" --root "$STATE" --name "$NAME" exec -- \
  python3 -B /work/storage-prepared/test/e2e/e2e run --workdir /work/storage-prepared \
  --suite storage --exclude storage.obs.sh --result /output/storage-result.json
```

重复的 `--suite` 和 `--include` 取并集，`--exclude` 按匹配文件名排除。未知或空选择
失败，运行尚未 prepare 的用例也失败。Suite 可以跨 owner。不要另加 owner/tag/
capability/fixture 选择机制。详见[测试组织](README_zh.md)。

<a id="6-perf-与-demo"></a>
## 5. Demo、源码检查与性能

`basic.demo.sh` 使用预备产品、镜像、SDK 和 helper 执行真实完整 Demo，并验证归属
和重复准备。它清除短流程和网络诊断控制项，调用者不能意外缩减验收。
[Demo](demo/DEMO_zh.md)另外提供 quick/full/interactive 用户流程，共用相同底层脚本，
但不声称覆盖额外的完整用例断言或整个 suite。

性能 harness 保留在源码 `test/perf/` 下，应使用[源码性能指南](../docs/perf_zh.md)，
不要假定 platform 包含这些路径。Unit/race/vet、UFFD 和 working-set 源码门禁保持
独立，smoke 结果不代表统计性能验收。

<a id="7-排错"></a>
## 6. 结果、清理与验收

Workbench 结果映射到 `$STATE/$NAME/output`，可变用例状态映射到
`$STATE/$NAME/work`。先在同一个实例内检查 root 所属的私有诊断，再决定分享内容。
检查完成后：

```bash
python3 "$WB" --root "$STATE" --name "$NAME" stop
python3 "$WB" --root "$STATE" --name "$NAME" cleanup
```

默认 cleanup 保留 work/output 和 journal；`--delete-output` 才显式删除，不应把
抹除证据作为默认重试步骤。缺失/损坏输入需要从已验证来源重新 prepare，而不是在
执行时远端替换。冲突资源应由实际 owner 处理。

开发候选结果必须记录脚本、产品、helper revision 和环境，不能冒充最终发布字节
验收。正式发布还需精确已发布身份、声明的原生覆盖及独立的无编译器/源码运行门禁。
Workbench 带有工具链，不能证明该独立门禁。失败与排除项必须保留可见。
