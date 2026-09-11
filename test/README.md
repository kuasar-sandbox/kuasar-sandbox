[English](README.md) | [简体中文](README_zh.md)

# Test organization

The project repository provides a shared test environment and aggregate release packaging. It does not keep copies of component-specific E2E tests.

- Each component owns its cases and `run_all.sh` under its own `test/e2e/`.
- `test/e2e/assemble.sh` assembles owner directories from the five component source trees.
- `test/e2e/run_all.sh` is the full entry point shared by source Integration E2E and release packages.
- `test/e2e/platform/` contains only tests of genuinely cross-component combinations.
- `test/perf/` and `test/demo/` remain owned by the project repository.

From the source workspace:

```bash
make build
make test-e2e
```

`make test-e2e` assembles `build/e2e-suite/` from the candidate component sources, then runs its `test/e2e/run_all.sh`. Aggregate releases produce the same layout from GitHub source archives at the selected component tags. Source PRs and release asset validation therefore use the same owner entry points.

For release-package usage and prerequisites, see [QUICKSTART.md](QUICKSTART.md).
