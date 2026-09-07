# Full-documentation migration inventory

Tracking: [#86](https://github.com/kuasar-sandbox/kuasar-sandbox/issues/86). This is an implementation ledger, not a language-check exemption list or a claim that translation is complete. Re-enumerate each target tree before final acceptance. Each row means the English default and, when preserving Chinese, its `_zh.md` counterpart.

| Repository | Initial Chinese-primary path | Implementation task |
| --- | --- | --- |
| kuasar-sandbox | `docs/kuasar-sandbox.md` | #88 |
| kuasar-sandbox | `docs/perf.md` | #88 |
| kuasar-sandbox | `docs/deployment.md` | #88 |
| kuasar-sandbox | `docs/ci.md` | #88 |
| kuasar-sandbox | `docs/release.md` | #88 |
| kuasar-sandbox | `test/README.md` | #88 |
| kuasar-sandbox | `test/QUICKSTART.md` | #88 |
| kuasar-sandbox | `test/demo/DEMO.md` | #88 |
| orchestrator | `docs/node.md` | kuasar-sandbox/orchestrator#316 |
| orchestrator | `docs/cluster.md` | kuasar-sandbox/orchestrator#316 |
| orchestrator | `docs/node-proxy.md` | kuasar-sandbox/orchestrator#316 |
| orchestrator | `docs/cluster-router.md` | kuasar-sandbox/orchestrator#316 |
| orchestrator | `docs/cluster-placer.md` | kuasar-sandbox/orchestrator#316 |
| orchestrator | `docs/node-resource.md` | kuasar-sandbox/orchestrator#316 |
| sandboxer | `docs/sandbox.md` | kuasar-sandbox/sandboxer#192 |
| sandboxer | `docs/sandbox-init.md` | kuasar-sandbox/sandboxer#192 |
| sandboxer | `docs/cloud-hypervisor.md` | kuasar-sandbox/sandboxer#192 |
| sandboxer | `native-deps/README.md` | kuasar-sandbox/sandboxer#192 |
| accelerator | `docs/manifest.md` | kuasar-sandbox/accelerator#104 |
| accelerator | `docs/cache.md` | kuasar-sandbox/accelerator#104 |
| accelerator | `docs/store.md` | kuasar-sandbox/accelerator#104 |
| connector | `docs/vswitch.md` | kuasar-sandbox/connector#40 |
| connector | `docs/tapfd.md` | kuasar-sandbox/connector#40 |
| guest-runtime | `docs/flatten.md` | kuasar-sandbox/guest-runtime#47 |
| guest-runtime | `docs/vmlinux.md` | kuasar-sandbox/guest-runtime#47 |
| guest-runtime | `docs/sandbox-runtime.md` | kuasar-sandbox/guest-runtime#47 |
| guest-runtime | `native-deps/README.md` | kuasar-sandbox/guest-runtime#47 |
| guest-runtime | `native-deps/docs/build.md` | kuasar-sandbox/guest-runtime#47 |

The previous English entry PRs, existing English design/example documents, community files, license-scope explanations and user-facing non-Markdown documentation are also subject to omission and consistency review. Existing complete English material need not be translated into Chinese. Upstream legal texts, third-party source comments and deliberately multilingual test data retain their original form.

Per-file evidence belongs in the implementation issues and [final acceptance #89](https://github.com/kuasar-sandbox/kuasar-sandbox/issues/89): source commit, PR, final head/merge commit, corrections, section coverage, link/code-example checks and limitations. Do not mark a repository complete based only on the number of `_zh.md` files.
