# kuasar-sandbox — complete build entry point for the platform.
#
# `make build` drives every sub-repo's build (via their Makefiles) and then
# assembles all platform runtime files into bin/$(TARGET_ARCH)/ (with native-arch
# symlinks under bin/). `make release` converges an explicitly selected
# release-v* version.
#
# Product E2E consumes a complete prebuilt release tree through one public
# prepare/run entry. Component source gates and helper builds are independent.

SHELL    := /bin/bash
ORG      := $(abspath $(CURDIR)/..)

# ---------------------------------------------------------------------------
# Architecture selection (identical normalization across kuasar-sandbox repos)
# ---------------------------------------------------------------------------
HOST_ARCH   := $(shell uname -m)
TARGET_ARCH ?= $(HOST_ARCH)
ifeq ($(TARGET_ARCH),amd64)
  override TARGET_ARCH := x86_64
endif
ifeq ($(TARGET_ARCH),arm64)
  override TARGET_ARCH := aarch64
endif
ifeq ($(TARGET_ARCH),x86_64)
else ifeq ($(TARGET_ARCH),aarch64)
else
  $(error unsupported TARGET_ARCH=$(TARGET_ARCH); supported: x86_64, aarch64)
endif
export TARGET_ARCH

BINDIR         := bin/$(TARGET_ARCH)
SBIN           := $(abspath $(BINDIR))
E2E_TOOL_DIR   := build/e2e-tools/$(TARGET_ARCH)
E2E_ZOT_BIN    ?= $(abspath $(E2E_TOOL_DIR)/zot)
E2E_VGW_BIN    ?= $(abspath $(E2E_TOOL_DIR)/versitygw)
E2E_SUITE_DIR  := $(abspath build/e2e-suite)
DEMO_DATA_DIR  ?= /var/lib/kuasar-demo
PYTHON_BIN     ?=
BIN_INPUTS_MANIFEST := release/bin-inputs.manifest
CI_TIMED       := ci/integration/ci-timed.sh
GO_REPOS       := accelerator sandboxer guest-runtime connector orchestrator
ZOT_VERSION    ?= v2.1.17

PERF_TARGETS := perf-sandbox perf-sandbox-manifest perf-sandbox-working-set perf-density perf-warmpool-dedup

.PHONY: all build collect e2e-zot e2e-versitygw e2e-tools assemble-e2e release verify-prebuilt vet test test-ci-tools test-release-tools test-perf-tools test-uffd-performance-gate clean help demo \
	        bench e2e-fixtures test-e2e perf dedup-report \
	        $(PERF_TARGETS)

all: build

# Full platform build. Sub-repo order matters: guest-runtime/native-deps leads
# (mkfs.erofs is needed by guest-runtime), sandboxer builds sandbox-init and
# cloud-hypervisor, then guest-runtime packs the guest erofs and builds
# flatten-ctl. Each sub-repo's `build` builds every binary it ships. After all
# sub-builds, `collect` assembles sub-repo artifacts under bin/$(TARGET_ARCH)/.
# Environment tools used only by tests are kept under build/e2e-tools/ and are
# packaged separately from product binaries in the platform E2E helper package.
build:
	$(CI_TIMED) build/guest-native $(MAKE) -C $(ORG)/guest-runtime/native-deps build
	$(CI_TIMED) build/accelerator $(MAKE) -C $(ORG)/accelerator build
	$(CI_TIMED) build/sandboxer $(MAKE) -C $(ORG)/sandboxer build
	$(CI_TIMED) build/guest-runtime $(MAKE) -C $(ORG)/guest-runtime build
	$(CI_TIMED) build/connector $(MAKE) -C $(ORG)/connector build
	$(CI_TIMED) build/orchestrator $(MAKE) -C $(ORG)/orchestrator build
	@$(CI_TIMED) build/collect $(MAKE) collect

e2e-zot:
	$(CI_TIMED) tools/zot env BINDIR="$(abspath $(E2E_TOOL_DIR))" TARGET_ARCH="$(TARGET_ARCH)" ZOT_VERSION="$(ZOT_VERSION)" bash ci/integration/ensure-zot.sh

e2e-versitygw:
	$(CI_TIMED) tools/versitygw env BINDIR="$(abspath $(E2E_TOOL_DIR))" TARGET_ARCH="$(TARGET_ARCH)" bash ci/integration/ensure-versitygw.sh

e2e-tools: e2e-zot e2e-versitygw

# Developer source preparation. The hosted artifact executor receives these
# helpers in its prepared workspace and never invokes this source target.
e2e-fixtures:
	$(MAKE) -C $(ORG)/sandboxer TARGET_ARCH="$(TARGET_ARCH)" E2E_FIXTURE_DIR="$(abspath $(E2E_TOOL_DIR))" e2e-usage-probe
	@if [ "$(TARGET_ARCH)" = x86_64 ]; then $(MAKE) -C $(ORG)/sandboxer TARGET_ARCH="$(TARGET_ARCH)" E2E_FIXTURE_DIR="$(abspath $(E2E_TOOL_DIR))" e2e-cgroup-fork-probe; fi
	bash $(ORG)/orchestrator/scripts/ci-e2e-build.sh fixtures "$(TARGET_ARCH)" "$(abspath $(E2E_TOOL_DIR))"

# Assemble bin/$(TARGET_ARCH)/ from each sub-repo's per-arch bin per the
# binary-input manifest. Native builds drop a bin/<name> symlink to the per-arch
# binary. Idempotent; missing inputs only warn.
collect:
	@rm -rf $(BINDIR); mkdir -p $(BINDIR)
	@while read repo name; do \
	  case "$$repo" in ''|\#*) continue ;; esac; \
	  [ -n "$$repo" ] || continue; \
	  src="$(ORG)/$$repo/bin/$(TARGET_ARCH)/$$name"; \
	  if [ -e "$$src" ]; then \
	    cp -f "$$src" "$(BINDIR)/$$name"; echo "  + $$name"; \
	  else \
	    echo "  ! missing: $$repo/bin/$(TARGET_ARCH)/$$name" >&2; \
	  fi; \
	done < $(BIN_INPUTS_MANIFEST)
	@if [ "$(HOST_ARCH)" = "$(TARGET_ARCH)" ]; then \
	   find bin -maxdepth 1 -type l -delete 2>/dev/null || true; \
	   while read repo name; do \
	     case "$$repo" in ''|\#*) continue ;; esac; \
	     [ -n "$$repo" ] || continue; \
	     [ -e $(BINDIR)/$$name ] && ln -sfn $(TARGET_ARCH)/$$name bin/$$name; \
	   done < $(BIN_INPUTS_MANIFEST); \
	 fi
	@echo "==> assembled $(BINDIR)/"

# Publish any missing selected component versions, then publish the aggregate.
RELEASE_VERSION ?=
PLATFORM_REF ?= $(shell git branch --show-current)
PLATFORM_SHA ?= $(shell git rev-parse HEAD)
release:
	@[ -n "$(RELEASE_VERSION)" ] || { echo "RELEASE_VERSION=release-vX.Y.Z is required" >&2; exit 1; }
	RELEASE_VERSION="$(RELEASE_VERSION)" PLATFORM_REF="$(PLATFORM_REF)" \
	  PLATFORM_SHA="$(PLATFORM_SHA)" python3 release/formal_coordinator.py

# ---------------------------------------------------------------------------
# Tests + benchmarks + perf (cross-repo)
# ---------------------------------------------------------------------------
assemble-e2e:
	rm -rf $(E2E_SUITE_DIR)
	bash test/e2e/assemble.sh $(E2E_SUITE_DIR) $(CURDIR) \
		$(ORG)/accelerator $(ORG)/connector $(ORG)/guest-runtime \
		$(ORG)/sandboxer $(ORG)/orchestrator

# The release tree contains products, cases, helpers and the public runner.
# Selection is a union of suites and filenames, minus excluded filenames.
RELEASE_DIR ?=
E2E_WORKDIR ?=
E2E_ARGS ?= --all --exclude storage.obs.sh
test-e2e:
	@[ -n "$(RELEASE_DIR)" ] && [ -n "$(E2E_WORKDIR)" ] || { echo "RELEASE_DIR and fresh E2E_WORKDIR are required" >&2; exit 1; }
	python3 -B "$(RELEASE_DIR)/test/e2e/e2e" prepare --release-dir "$(RELEASE_DIR)" --workdir "$(E2E_WORKDIR)" --arch "$(TARGET_ARCH)" $(E2E_ARGS)
	sudo -n python3 -B "$(E2E_WORKDIR)/test/e2e/e2e" run --workdir "$(E2E_WORKDIR)" --arch "$(TARGET_ARCH)" $(E2E_ARGS)

# Aggregate releases validate the already-published archives. This target never
# invokes a component build; bin/<arch>/ must be populated by the release fetcher.
verify-prebuilt:
	@while read repo name; do \
		case "$$repo" in ''|\#*) continue ;; esac; \
		[ -f "$(SBIN)/$$name" ] || { echo "missing prebuilt release input: $$name" >&2; exit 1; }; \
	done < $(BIN_INPUTS_MANIFEST)

# perf harnesses living in this repo (cross-repo binary use).
perf-sandbox:
	@[ -n "$(E2E_WORKDIR)" ] || { echo "E2E_WORKDIR must contain prepared lifecycle/manifest-boot inputs" >&2; exit 1; }
	E2E_WORKDIR="$(E2E_WORKDIR)" bash test/perf/sandbox-perf.sh
perf-sandbox-manifest: build
	BIN=$(SBIN) bash test/perf/sandbox-perf-manifest.sh
perf-sandbox-working-set: build
	BIN=$(SBIN) bash test/perf/sandbox-perf-working-set.sh
perf-density: build
	BIN=$(SBIN) bash test/perf/density-perf.sh
perf-warmpool-dedup: build
	BIN=$(SBIN) bash test/perf/warmpool-dedup.sh

# Aggregate perf: accelerator's perf-cache + this repo's perfs.
perf: build
	$(MAKE) -C $(ORG)/accelerator perf-cache
	$(MAKE) $(PERF_TARGETS)

# Go micro-benchmarks across every Go sub-repo.
bench:
	@for r in $(GO_REPOS); do echo "== bench $$r =="; $(MAKE) -C $(ORG)/$$r bench || exit 1; done

# Dedup analysis report (lives in accelerator).
dedup-report:
	$(MAKE) -C $(ORG)/accelerator dedup-report

# e2b end-to-end demo — the "try it" walkthrough driven by the unmodified e2b CLI
# (build template → boot microVM → exec → pause/resume → kill). DEMO_PAUSE=1 to
# step through and drive the CLI from another terminal; see test/demo/DEMO.md.
demo:
	@test -n "$(PYTHON_BIN)" || { echo "set PYTHON_BIN=/absolute/path/to/pinned-venv/bin/python" >&2; exit 1; }
	@case "$(PYTHON_BIN)" in /*) ;; *) echo "PYTHON_BIN must be absolute" >&2; exit 1 ;; esac
	@test -x "$(PYTHON_BIN)" || { echo "PYTHON_BIN must be executable" >&2; exit 1; }
	$(MAKE) build e2e-tools
	sudo -n env DEMO_DATA_DIR="$(DEMO_DATA_DIR)" BIN="$(SBIN)" ZOT_BIN="$(E2E_ZOT_BIN)" VGW_BIN="$(E2E_VGW_BIN)" bash test/demo/demo_prep.sh
	sudo -n env DEMO_DATA_DIR="$(DEMO_DATA_DIR)" BIN="$(SBIN)" PYTHON_BIN="$(PYTHON_BIN)" DEMO_QUICKSTART="$(DEMO_QUICKSTART)" DEMO_PAUSE="$(DEMO_PAUSE)" DEMO_KEEP="$(DEMO_KEEP)" DEMO_NETDIAG="$(DEMO_NETDIAG)" bash test/demo/demo_e2b.sh

# ---------------------------------------------------------------------------
# Sub-repo vet/test/clean aggregates
# ---------------------------------------------------------------------------
vet:
	@for r in $(GO_REPOS); do echo "== vet $$r =="; $(MAKE) -C $(ORG)/$$r vet || exit 1; done

test:
	@for r in $(GO_REPOS); do echo "== test $$r =="; $(MAKE) -C $(ORG)/$$r test || exit 1; done

test-ci-tools:
	bash ci/integration/test-ci-tools.sh
	PYTHONDONTWRITEBYTECODE=1 python3 ci/integration/test-source-owner.py
	PYTHONDONTWRITEBYTECODE=1 python3 test/e2e/test_e2e_runner.py
	PYTHONDONTWRITEBYTECODE=1 python3 test/e2e/test_offline_inputs.py
	PYTHONDONTWRITEBYTECODE=1 python3 workbench/test_workbench.py
	PYTHONDONTWRITEBYTECODE=1 python3 test/e2e/platform/lib/test_failure_diagnostics.py
	KUASAR_RUNTIME_READER="$${KUASAR_RUNTIME_READER:-$(ORG)/guest-runtime/scripts/release-runtime-payloads.py}" \
		PYTHONDONTWRITEBYTECODE=1 python3 ci/integration/test-artifacts.py

test-release-tools:
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s release -p 'test_workbench_*.py'
	PYTHONDONTWRITEBYTECODE=1 python3 release/test_e2e_package.py
	PYTHONDONTWRITEBYTECODE=1 python3 release/test-environment-tools.py
	bash test/demo/test_demo_safety.sh
	PYTHONDONTWRITEBYTECODE=1 python3 test/demo/test_demo_state_assertions.py
	PYTHONDONTWRITEBYTECODE=1 python3 test/demo/test_demo_process_startup.py
	PYTHONDONTWRITEBYTECODE=1 python3 test/demo/test_demo_network_names.py
	PYTHONDONTWRITEBYTECODE=1 python3 test/demo/test_demo_files_storage.py
	PYTHONDONTWRITEBYTECODE=1 python3 test/demo/test_demo_build_selection.py
	PYTHONDONTWRITEBYTECODE=1 python3 test/demo/test_prepared_demo.py
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest release/test_documentation_package.py
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest release/preview_selection_test.py
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest release/preview_coordinator_test.py
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest release/preview_gc_test.py
	bash release/test-release.sh

test-perf-tools:
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s test/perf -p '*_test.py'
	bash test/perf/uffd_performance_gate_test.sh
	bash test/perf/working_set_tap_test.sh
	bash test/perf/working_set_netns_test.sh

test-uffd-performance-gate:
	$(CI_TIMED) perf/uffd-gate env ORG="$(ORG)" bash test/perf/uffd-performance-gate.sh

clean:
	@for r in $(GO_REPOS); do $(MAKE) -C $(ORG)/$$r clean 2>/dev/null || true; done
	rm -rf bin build

help:
	@echo "kuasar-sandbox — platform build entry. Targets:"
	@echo "  build         build every sub-repo + assemble bin/\$$(TARGET_ARCH)/ (multi-min cold)"
	@echo "  collect       re-assemble bin/\$$(TARGET_ARCH)/ from existing sub-repo outputs"
	@echo "  e2e-tools     ensure local test environment tools under build/e2e-tools/\$$(TARGET_ARCH)/"
	@echo "  release       publish a selected aggregate (RELEASE_VERSION=release-vX.Y.Z)"
	@echo "  assemble-e2e  assemble component-owned and platform-owned suites"
	@echo "  test-e2e      prepare and run RELEASE_DIR inputs in a fresh E2E_WORKDIR"
	@echo "  demo          source-only Demo (requires PYTHON_BIN; release users follow docs/quickstart.md)"
	@echo "  perf          aggregate: accelerator perf-cache + this repo's perf-sandbox/-manifest/-density"
	@echo "  bench         Go micro-benchmarks across every Go sub-repo"
	@echo "  dedup-report  delegate to accelerator"
	@echo "  vet / test    drive each Go sub-repo's vet/test target"
	@echo "  test-ci-tools validate CI cache/timing helpers"
	@echo "  test-release-tools validate selections, packages, checksums, and publishers"
	@echo "  test-perf-tools validate performance report generators"
	@echo "  test-uffd-performance-gate enforce source UFFD A/B/C performance bounds"
	@echo "  clean         clean every sub-repo + this repo's bin/ build/"
	@echo "  TARGET_ARCH   x86_64 (default) | aarch64  (exported to sub-makes)"
