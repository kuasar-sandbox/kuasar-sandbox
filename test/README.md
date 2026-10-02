[English](README.md) | [简体中文](README_zh.md)

# Test organization

Product E2E follows one contract: **prebuilt products → prepare → `<suite>.<case>.sh` → shared public runner**. The complete filename is the case ID; its first segment is the suite. The nine suites are `basic`, `storage`, `image`, `network`, `sandbox`, `snapshot`, `orchestrator`, `builder` and `telemetry`.

Components maintain their own `test/e2e/cases/` and low-level `test/e2e/lib/` helpers. Platform owns `test/e2e/platform/cases/basic.demo.sh`. Source-time assembly copies exact selected test revisions into one flat `test/e2e/cases/` directory and namespaces helpers under `test/e2e/lib/<owner>/`. Duplicate IDs, unknown suites and owner runners are rejected. Ownership does not change public selection.

The platform archive carries the shared `test/e2e/e2e` runner and prebuilt helpers for both architectures. The earlier source/release build fetches the complete Demo SDK wheel closure using `test/demo/requirements.lock` and packages both architecture wheelhouses. Preparation resolves images and installs the SDK only from those hash-locked local wheels (`--no-index --find-links`, `--require-hashes`), then records package/version/wheel identities, installed file hashes, modes and image content IDs. Missing or changed wheels fail closed. Execution consumes that immutable workspace. It does not build products/helpers, discover sibling source trees, pull replacement images or use host helper binaries. Tested Build, flatten, snapshot and publication operations remain real.

The release package includes the canonical cases, explicitly listed runtime libraries and actual Demo inputs. Source unit tests and performance tools remain in the source tree. Selected bilingual user guides are under `guide/`; the accelerator and guest-runtime E2E README pairs remain under `test/e2e/<owner>/`. The thin host launcher and instructions are under `workbench/`. Helpers and wheelhouses keep their existing contracts.

```bash
python3 /release/test/e2e/e2e list --suite storage
python3 /release/test/e2e/e2e prepare --release-dir /release --workdir /tmp/kuasar-prepared --suite storage --exclude storage.obs.sh
sudo python3 /tmp/kuasar-prepared/test/e2e/e2e run --workdir /tmp/kuasar-prepared --suite storage --exclude storage.obs.sh
```

`--suite` and `--include` form a union; `--exclude` removes full filenames. Unknown selectors and empty selections fail. `--all` includes credentialed `storage.obs.sh`; normal public CI explicitly excludes it. There are no owner, tag, capability or fixture-graph selectors. The Makefile wrapper requires `RELEASE_DIR`, a fresh `E2E_WORKDIR` and optional `E2E_ARGS`.

Preparation accepts `--deps-dir` / `E2E_DEPS_DIR` for verified local image archives and `--offline` / `E2E_OFFLINE=1` to prohibit dependency downloads. Unconfigured environments remain online. Local matches are validated without remote freshness checks; invalid matches never trigger remote repair. External requests come from one case/architecture function, which build-time input collection can also call. See [local and offline preparation](QUICKSTART.md#local-and-offline-preparation) for precedence, identity evidence and failure behavior.

Native x86_64 and aarch64 use the same public `--all --exclude storage.obs.sh`
selection. Preparation requires the real host, product, helper and image
architectures to agree, including both orchestrator fixture variants. Selected
helpers are checked before expensive generation; `sandbox.cgroup.sh` requires
the exact-source static no-libc probe in that architecture's `helpers.json`.
Unselected helpers are not prerequisites. Demo consumes the prepared local
native image identity, never a replacement or a second ARM digest list.

CI resolves exact test filenames and selects broad suites for changed owners. Source builds finish before preparation. Products retain their own source closure while test scripts and helper binaries use independent test pins. CI preparation runs in an isolated runtime with no Go/Rust toolchains or component source trees. Every suite shard invokes the same packaged runner; storage, snapshot and ARM image acceptance also use an isolated runtime. Preparation and execution image IDs and environment checks are bound to their results. Results require every selected case's successful exit and one unchanged prepared input identity. Hosted ARM CI selects accelerator and guest-runtime non-KVM cases; exclusions are explicit. A static lane has zero product cases and is never product E2E acceptance.

Unit, race, vet, source helper, UFFD performance and working-set gates remain independent. `test/perf/warmpool-dedup.sh` retains the warm-pool characterization and its data/measurement assertions under `make perf-warmpool-dedup`; it is not a correctness suite. Component source checks stay in their owning repositories.

The platform assertion owners are:

| Contract | Maintained owner |
| --- | --- |
| Foreign listener refusal and survival; exact owned reset | `basic.demo.sh` plus Demo safety helpers |
| Repeated prep, root ownership, 0700 directory and 0600 state | `basic.demo.sh` |
| Real Build/COPY, Quick Start lifecycle, SDK exec/files, template fan-out, migration and data | `basic.demo.sh` invoking the documented `test/demo/demo_e2b.sh` flow |
| Stop/start durable prep, stale sockets and cleanup failure status | `basic.demo.sh` plus Demo process/state source tests |
| Exact wheel-only SDK, isolated import and immutable inputs | Shared preparation and `test-e2e-tool-paths.py` |
| Bounded recovery diagnostics, redaction, original exit/cleanup and descriptor closure | Platform diagnostics helpers; `snapshot.read-recovery.sh` and `orchestrator.cluster-recovery.sh` |
| Warm-pool dedup workload and measurements | `test/perf/warmpool-dedup.sh` |

See [QUICKSTART.md](QUICKSTART.md) for release inputs, prerequisites and acceptance evidence.
