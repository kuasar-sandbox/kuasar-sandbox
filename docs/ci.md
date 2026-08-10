# BMS CI

## 1. 概述

platform 维护 BMS 的可信控制面与唯一执行 workflow。五个组件仓只保留事件触发和参数
wrapper,并从 platform `main` 引用 `.github/workflows/bms-entry.yml`;该入口再从同一个
platform `main` revision 调用 `.github/workflows/bms-e2e.yml`。公共准入、runner 初始化、
源码缓存、native cache、完整 E2E 和精确发布资产验证不再复制到各仓。

BMS 有两个明确模式:

- `source`:验证一个仓的 PR integration commit 与其余仓当前 `main` 的组合;
- `exact-assets`:验证 aggregate workflow 已下载并校验的发布 archive,不重新构建。

## 2. Source 模式

各仓 `main` 上的可信 wrapper 使用 `pull_request_target` 接收事件,不执行候选仓提供的
workflow。同仓非 draft PR 自动准入;fork PR 由 GitHub-hosted 控制 job 使用只读 App token
查询当前组织成员关系,仅 `active` 的 owner/member 自动准入。控制 job 同时重新查询当前 PR,
校验 GitHub 生成的 two-parent integration commit,并在该 commit 上把
`kuasar/bms-exact-head` 置为 `pending`。事件中的 `author_association` 不作为私有组织成员
身份来源。draft PR 不取得自托管 runner,其 exact-head 保持 `pending`;转为 ready 后由新的
事件自动运行完整 BMS。外部 fork、冲突或已经变化的事件拒绝准入。

其余五仓 revision 通过一次 GitHub GraphQL 查询解析,候选仓 revision 替换为已准入的
integration commit。source 模式没有 `main` push 或手工 dispatch 入口。

可复用 workflow 验证:

1. 控制面与执行实现都解析自 `kuasar-sandbox/platform` 的 `main`,运行中记录解析后的完整 SHA;
2. `pull_request_target` event、当前 PR 与 candidate/base/head 输入完全一致;
3. candidate 是以 base/head 为两个父提交的 integration commit;
4. 当前开放 PR 的 base/head/merge commit 在执行前与结束后均未变化;
5. fork 准入 token 只请求组织 `Members: read`,仅存在于 GitHub-hosted 控制 job;
6. 自托管 job 的 `GITHUB_TOKEN` 只有 `Contents: read` 和 `Pull requests: read`;
7. source App token 只有六仓 `Contents: read`,并在执行候选代码前撤销。

结束控制 job 在 GitHub-hosted runner 再次查询 PR。只有 BMS 成功且当前 integration commit
仍与准入值完全一致时,它才把同一个 `kuasar/bms-exact-head` 状态置为 `success`;测试失败、
取消、跳过或 PR 已变化都不会生成可用于合入的成功状态。GitHub 自带的
`pull_request_target` workflow check 绑定 PR head commit,不能代替这个 integration-commit
状态。

platform tooling 与 source 使用只读 App token 从 GitHub 官方 archive API 按完整 SHA 获取,
不依赖 Git smart HTTP;二者 SHA 相同时直接复用已解包的 trusted tree。归档必须只有一个顶层
目录、不得包含路径穿越或符号链接等非普通条目。五个组件的源码归档缓存在
`/var/cache/kuasar/sources/<repo>/<sha>.tar.gz`。命中时校验 SHA-256 和 tar 结构;miss 从
GitHub 官方 tarball 下载,失败时改用官方 zipball 并本地转换。每仓以 `flock`
串行维护,保留最近使用的 32 个 revision。token 在执行候选代码前显式撤销。

随后创建五组件 `go.work`,恢复或构建 native cache,并从候选 platform 源码运行:

```bash
make -C src/platform test-release-tools test-perf-tools
make -C src/platform test-e2e
```

每个组件在自己的 `test/e2e/` 维护用例与唯一入口 `run_all.sh`。platform source BMS 将五个
组件源码与 `platform/test/e2e/platform/` 中真正跨组件组合本身的用例组装为:

```text
test/e2e/run_all.sh
test/e2e/accelerator/run_all.sh
test/e2e/connector/run_all.sh
test/e2e/guest-runtime/run_all.sh
test/e2e/sandboxer/run_all.sh
test/e2e/orchestrator/run_all.sh
test/e2e/platform/run_all.sh
```

顶层入口按 owner 顺序调用六个入口。某组件是候选仓时,组装目录直接采用该 PR integration
commit 的 `test/e2e/`,所以特性实现与其 E2E 在同一个 PR 评审,组件 PR 的完整 BMS 结果也以
该组件 `run_all.sh` 在统一环境中通过为准。测试需要其他仓二进制不改变所有权:platform 只
提供完整 `BIN`、zot、versitygw、KVM 与系统服务环境,不复制用例源码。

完整 source BMS 还会固定执行 1 轮 working-set smoke,覆盖 A/B/C/D 以及本地加密
`off/auto × cold/warm`。30 轮 canonical 测量用于生成稳定的描述性性能报告,不作为
workflow 的独立模式或 PR 必跑轮数。

## 3. Exact-assets 模式

aggregate prepare job 上传包含以下内容的短期 Actions artifact:

```text
assets/             platform 包、六个组件 archive、统一 SHA256SUMS
selection.tsv       workflow 内部版本选择,不上传到 GitHub Release
release-notes.md    GitHub Release 页面说明
```

BMS 重新验证固定资产集合、SHA-256、platform 包范围、tar 安全路径和跨包覆盖,然后解压到
`release-install/`。组件 archive 只提供运行制品;组件文档与 E2E 已由 aggregate prepare
从清单所选 tag 的 GitHub 源码归档收集进 platform 包。执行入口来自 platform archive 本身:

```bash
cd release-install
BIN=$PWD/bin bash test/e2e/run_all.sh
```

该模式不读取组件 `main`:文档和用例与二进制一样绑定清单所选 tag。它不调用
Go/Rust/native build,也不会重新编译 vmlinux。通过后 publish job 原样使用此前的 artifact。

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
versitygw 从 runner 固定工具目录链接到当前 workspace,不进入发布包。`kuasar-e2e` runner
group 对仓库保持 `visibility=all`,但只允许
`kuasar-sandbox/platform/.github/workflows/bms-e2e.yml@refs/heads/main` 分配自托管 runner。
各仓 fork workflow 的 secrets 转发关闭;需要 App secret 的准备步骤只存在于 base 仓可信
workflow,且候选代码执行前相关 token 已撤销。

## 6. Run artifacts

每次 BMS 上传 `ci-metadata-<run>-<attempt>`。source 模式通常包含:

- `run.tsv`:模式、候选仓、PR 与 candidate/base/head SHA;
- `revisions.tsv`:platform 测试框架与五组件的精确 revision;
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
