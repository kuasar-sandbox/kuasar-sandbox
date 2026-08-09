# 测试组织

platform 提供统一测试环境和发布聚合,不保存组件特性 E2E 的副本。

- 各组件在自己的 `test/e2e/` 维护用例和 `run_all.sh`;
- `test/e2e/assemble.sh` 从五个组件源码组装 owner 目录;
- `test/e2e/run_all.sh` 是源码 BMS 与发布包共同使用的完整入口;
- `test/e2e/platform/` 只维护真正跨组件组合本身的用例;
- `test/perf/` 与 `test/demo/` 继续由 platform 维护。

源码工作区运行:

```bash
make build
make test-e2e
```

`make test-e2e` 使用候选组件源码组装 `build/e2e-suite/`,再执行其中的
`test/e2e/run_all.sh`。聚合发布从所选组件 tag 的 GitHub 源码归档生成相同布局,因此源码
PR 与 exact-asset BMS 使用同一组 owner 入口。

发布包使用方式及环境前置见 [QUICKSTART.md](QUICKSTART.md)。
