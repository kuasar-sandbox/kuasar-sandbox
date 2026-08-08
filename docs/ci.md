# BMS CI

## 1. 概述

platform 维护唯一的 BMS 可复用 workflow。五个组件仓只保留事件触发和候选参数 wrapper,
并以完整 platform commit SHA 引用 `.github/workflows/bms-e2e.yml`;公共 runner 初始化、源码
缓存、native cache、完整 E2E 和精确发布资产验证不再复制到各仓。

BMS 有两个明确模式:

- `source`:验证一个仓的 PR integration commit 或 `main` commit 与其余仓当前 `main` 的组合;
- `exact-assets`:验证 aggregate workflow 已下载并校验的发布 archive,不重新构建。

## 2. Source 模式

同仓非 draft PR 使用 GitHub 生成的 two-parent integration commit。其余五仓 revision 通过
一次 GitHub GraphQL 查询解析,候选仓 revision 替换为 integration commit。`main` push 使用
该 push 的精确 SHA。私有 fork 不携带 secret 直接执行;评审后由维护者从可信 wrapper 的
`workflow_dispatch` 输入当前 PR number、candidate/base/head SHA。

可复用 workflow 验证:

1. 公共实现来自 `kuasar-sandbox/platform` 的固定 SHA;
2. candidate、base 和 head 都是完整 SHA;
3. candidate 是以 base/head 为两个父提交的 integration commit;
4. 当前开放 PR 的 base/head/merge commit 仍与输入一致;
5. source App token 只有六仓 `Contents: read`。

源码归档缓存在 `/var/cache/kuasar/sources/<repo>/<sha>.tar.gz`。命中时校验 SHA-256 和 tar
结构;miss 从 GitHub 官方 tarball 下载,失败时改用官方 zipball 并本地转换。每仓以 `flock`
串行维护,保留最近使用的 32 个 revision。token 在执行候选代码前显式撤销。

随后创建五组件 `go.work`,恢复或构建 native cache,并从候选 platform 源码运行:

```bash
make -C src/platform test-release-tools test-perf-tools
make -C src/platform test-e2e
```

working-set 性能模式只运行 `make perf-sandbox-working-set`,并使用独立确认字符串触发。

## 3. Exact-assets 模式

aggregate prepare job 上传包含以下内容的短期 Actions artifact:

```text
assets/             platform 包、六个组件 archive、统一 SHA256SUMS
selection.tsv       workflow 内部版本选择,不上传到 GitHub Release
release-notes.md    GitHub Release 页面说明
```

BMS 重新验证固定资产集合、SHA-256、platform 包范围、tar 安全路径和跨包覆盖,然后解压到
`release-install/`。执行入口来自 platform archive 本身:

```bash
cd release-install
BIN=$PWD/bin bash test/e2e/run_all.sh
```

组件 archive 中的 accelerator、connector、guest-runtime 和 orchestrator 模块 E2E 与
platform 跨仓测试合并到同一目录。该模式不读取组件 `main`,不调用 Go/Rust/native build,
也不会重新编译 vmlinux。通过后 publish job 原样使用此前的 artifact。

## 4. Native cache

`ci/native-cache/native-cache.sh restore-or-build` 处理:

- guest `vmlinux`;
- `mkfs.erofs` 与 `fsck.erofs`;
- guest `envd`;
- RocksDB headers 与 `librocksdb.a`;
- patched `cloud-hypervisor`。

缓存路径为 `/var/cache/kuasar/native/v1/<arch>/<component>/<input-hash>/`。input hash 覆盖
构建脚本、patch/config、上游摘要、架构、Go/Cargo/C/C++ 工具链和 pkg-config 解析结果。
条目通过 staging、校验和及原子 rename 发布;命中恢复前重新校验 descriptor、payload 和
tar 路径。损坏条目失败,不会在原目录修补。

同 key 构建和恢复持有条目锁。每组件默认保留最近使用的 4 个 key,且只回收超过保护期并能
非阻塞取得锁的条目。缓存测试入口:

```bash
make -C platform test-ci-tools
```

## 5. Runner 与网络

runner 安装资料位于 `ci/runner/`。自托管 job 只有收窄后的 read token,每日协调 token 与
`contents:write` token 只在 GitHub-hosted job 使用。runner 代理属于部署配置,不写入仓库
workflow;Go、Rust、Python、Linux kernel 与常用容器镜像使用公开中国大陆镜像降低网络抖动。

每次 job 开始会停止遗留 sandbox systemd unit、删除测试 tap 并重载 systemd。常用 zot 与
versitygw 从 runner 固定工具目录链接到当前 workspace,不进入发布包。

## 6. Run artifacts

每次 BMS 上传 `ci-metadata-<run>-<attempt>`。source 模式通常包含:

- `run.tsv`:模式、候选仓、PR 与 candidate/base/head SHA;
- `revisions.tsv`:五组件精确 revision;
- `source-cache.tsv`:源码缓存命中和摘要;
- `native-cache.tsv`:native key、命中和耗时;
- `timings.tsv`:构建与测试阶段资源数据;
- UFFD 与 working-set 报告(仅对应测试运行时)。

exact-assets 模式只记录 run 与测试输出,不创建伪造的源码或 native cache 元数据。发布版本
组合由 platform main 选择、组件 tag 和 GitHub Release notes 表达。

## 7. See Also

- [release.md](release.md):发布资产、aggregate BMS 和权限边界;
- [deployment.md](deployment.md):BMS 所需系统服务与运行环境;
- [../ci/runner/README.md](../ci/runner/README.md):runner 安装与维护;
- [../test/QUICKSTART.md](../test/QUICKSTART.md):E2E 前置条件与排错。
