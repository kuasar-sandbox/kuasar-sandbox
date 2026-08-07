# Cross-repo e2e / perf harness

System-level integration and performance suites for the kuasar-sandbox
platform. This directory is the umbrella test home under
`platform` because these cases span multiple sibling repos
and consume the assembled `bin/<arch>/` artifact set.

## Entry Points

From `platform`:

```bash
make build
make test-e2e
make test-e2e-sandbox-cold
make perf
make demo
```

From the org root:

```bash
make -C platform build
make -C platform test-e2e
make -C platform release
```

`make build` drives `guest-runtime/native-deps`, `accelerator`, `sandboxer`,
`guest-runtime`, `connector`, and `orchestrator`, then collects their artifacts
into `platform/bin/<arch>/`. In release archives, runnable
scripts are staged under `test/e2e`, `test/perf`, and `test/demo`, so they keep
the same `SCRIPT_DIR/../../bin` binary discovery convention after component
packages are extracted into one directory. `BIN=/path/to/bin` can override it.

## Suites

- `e2e/` — cold boot, tapfd networking, snapshot/restore, manifest/cache/store,
  node orchestration, e2b-compatible execution, cluster stub coverage, and real
  cluster-to-microVM coverage.
- `perf/` — sandbox latency, manifest path latency, and density harnesses.
- `demo/` — e2b SDK walkthrough driven by `demo_prep.sh` and `demo_e2b.sh`.

See `QUICKSTART.md` for release-package usage and prerequisite details.
