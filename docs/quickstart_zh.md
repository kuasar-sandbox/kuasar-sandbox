[English](quickstart.md) | [简体中文](quickstart_zh.md)

# 快速开始

使用匹配的聚合发布版与 workbench，通过未修改的 E2B Python SDK 运行真实 Kuasar
MicroVM。这是推荐的首次体验路径；原生生产部署不依赖 workbench，见
[部署指南](deployment_zh.md)。

短 Demo 构建快照模板、创建沙箱、执行命令、读写文件、检查网络、暂停恢复并销毁沙箱。
这是首次体验演示，**不是完整 E2E 或发布验收**。

## 1. 获取一个支持该流程的发布版

需要原生 Linux x86_64 或 aarch64、本机 rootful Docker Engine、Python 3.9+ 及
Docker 访问权限。这是可信管理员环境，不支持 Docker Desktop、远端 Docker 端点
或架构模拟。真正运行 MicroVM 还需要可读写的 KVM 与所需内核功能；workbench
不会模拟缺失硬件或修改宿主内核策略。

镜像提供 systemd、私有 Docker/containerd、产品用户态依赖库、Python 3.12 和普通
工具。仅启动 workbench 时，宿主**不需要** Demo SDK、产品所需 glibc 版本、Go/Rust，
也不要求 systemd 为宿主 PID 1。为宿主和其他工作负载保留 CPU、内存及磁盘余量。
示例为 workbench 分配 4 CPU、12 GiB，不代表通用最小值或完整测试容量保证；
Demo Builder 本身请求 2 vCPU 和 6 GiB capacity。

先执行一次[获取匹配的聚合发布版](download_zh.md)：选择具体已发布版本，验证交付合同
及本机架构资产，解包产品和材料，再单独导入 workbench。完成后得到 `RELEASE`
（绝对发布目录）、`IMAGE` 和 `ARCH`。下面继续使用同一个**宿主 Bash shell**中的
这些变量。只使用该版本配套的启动器、Demo、helper、wheelhouse 和产品；缺少预备
Demo 适配器的旧版本应使用其包内指南，不得从 `main` 补脚本。

## 2. 启动私有系统环境

以下命令在**宿主机**执行。状态目录应位于发布树之外，并由调用 UID 拥有。
不要切换调用用户，也不要只为部分启动器命令添加 `sudo`。已 cleanup 的实例应换新名称。

```bash
STATE="$PWD/kuasar-workbench-state"
NAME="demo-$(date +%s)"
WB="$RELEASE/workbench/workbench"
python3 "$WB" check --image "$IMAGE" --mode system
python3 "$WB" --root "$STATE" --name "$NAME" start \
  --image "$IMAGE" --mode system --inputs "$RELEASE" \
  --cpus 4 --memory-gib 12 --network bridge
```

`check` 检查宿主和镜像兼容性；`start` 检查私有 systemd/daemon 并报告硬件能力。
它们均不能证明 Demo 已通过。实例使用私有网络及服务：宿主 `127.0.0.1` 不等于
workbench 的 `127.0.0.1`。不要用宿主网络或挂载宿主 Docker socket 来规避此区别。

## 3. 准备本地输入

外层命令在**宿主机**运行，`exec --` 之后的命令在 **workbench 内**执行。
`/inputs/release` 只读；公共 prepare 使用所选镜像归档、helper 和 hash-locked SDK
wheel 创建全新的不可变 `/work/prepared`。不要在 `/inputs/release` 下创建虚拟环境，
也不要在线安装另一套 SDK。

```bash
python3 "$WB" --root "$STATE" --name "$NAME" exec -- \
  python3 -B /inputs/release/test/e2e/e2e prepare \
  --release-dir /inputs/release --workdir /work/prepared --arch "$ARCH" \
  --include basic.demo.sh --deps-dir /opt/workbench/deps --offline
```

`--offline` 在获取制品之后禁止依赖下载，不禁止本地 Registry/Store/Proxy 通信，
也不代表随后的 Demo 不需要网络。**保留 `--network bridge`：短 Demo 同样检查真实
Internet 出站。** 不得用 `DEMO_NETDIAG` 把断言失败转成成功。缺少输入时准备失败，
不会静默替换。

## 4. 运行第一个沙箱

```bash
python3 "$WB" --root "$STATE" --name "$NAME" exec -- \
  python3 -B /work/prepared/test/demo/prepared.py run \
  --workdir /work/prepared --data-dir /work/kuasar-demo-first --quick
```

适配器验证预备输入、加载精确原生镜像，使用本地 SDK/helper 环境调用现有 Demo 脚本。
沙箱命令在 **Guest 内**执行；可变状态和保留日志位于 `/work/kuasar-demo-first`，
不写入预备输入树。

成功要求真实模板、MicroVM、命令/文件、网络、暂停恢复及销毁断言全部通过，且退出码
为零。Demo 会打印阶段，但结束后不保留生产节点服务。重复执行同一命令，可复用持久
准备层并生成新的运行身份。

完整 COPY、模板扇出、迁移及交互观察见 [Demo](../test/demo/DEMO_zh.md)；完整常规
用例选择和结果 JSON 见[发布验证](../test/QUICKSTART_zh.md)。

## 5. 查看结果并清理

适配器将可安全保留的 Demo 日志放在 `/work/kuasar-demo-first/results/`，准备日志
位于其 `logs/`。私有诊断可能包含敏感状态，分享前应在本机检查。它们映射到宿主
`$STATE/$NAME/work/kuasar-demo-first`；启动诊断位于 `$STATE/$NAME/output`。
通过同一个 workbench 实例查看 root 所属的数据。

先停止所属 Demo 准备服务，再停止 workbench，最后移除容器、网络与 daemon 数据：

```bash
python3 "$WB" --root "$STATE" --name "$NAME" exec -- \
  python3 -B /work/prepared/test/demo/prepared.py stop \
  --workdir /work/prepared --data-dir /work/kuasar-demo-first
python3 "$WB" --root "$STATE" --name "$NAME" stop
python3 "$WB" --root "$STATE" --name "$NAME" cleanup
```

`cleanup` 保留 work/build/home/journal/output；`cleanup --delete-output` 才会显式
删除它们，执行前应保存所需结果。Demo 的 `reset` 也会删除该 Demo 的日志和数据，
因此不作为默认故障恢复步骤。停止但未清理的实例可以按原镜像、模式、挂载和预算重启；
已清理的实例需要新名称。完整生命周期语义见 [Workbench](../workbench/README_zh.md)。

## 故障排查

历史合同或缺少适配器的报错表示所选版本尚不提供此流程。应显式选择符合要求的已发布
版本，或按历史原生指南执行；不要静默切换通道或混用脚本版本。

缺少 KVM/内核能力时，选择满足要求的原生主机。prepare 失败时修复其明确报告的
缺失/损坏输入，再使用全新的预备目录。workbench 内监听器、unit 或网络冲突时，
检查该实例先前的 Demo，不要停止无关宿主服务。Demo 或清理非零退出表示失败，
不是成功跳过。
