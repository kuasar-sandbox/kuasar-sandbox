#!/usr/bin/env python3
"""Build the planned product delta once using the existing owner recipes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile

import artifacts

ROOT = Path(__file__).resolve().parents[2]
LIBRARIES = {"sandboxer": ("accelerator", "connector"), "guest-runtime": ("accelerator",),
             "orchestrator": ("accelerator", "connector", "sandboxer")}
NATIVE = {"cache-ctl": "rocksdb", "mkfs.erofs": "erofs", "cloud-hypervisor": "cloud-hypervisor", "vmlinux": "vmlinux"}


def run(command, *, cwd=None, environment=None):
    subprocess.run([str(arg) for arg in command], cwd=cwd, env=environment, check=True)


def checkout(repository, sha, destination):
    artifacts.require(not destination.exists(), "build sources require a fresh isolated directory")
    artifacts.require(repository.startswith("kuasar-sandbox/") and len(sha) == 40, "invalid exact source")
    # All admitted callers and companions are public. No App key, installation
    # credential, checkout token, user hooks or private source cache is needed.
    run(["git", "init", "--quiet", "--template=", destination])
    run(["git", "-C", destination, "config", "core.hooksPath", "/dev/null"])
    run(["git", "-C", destination, "remote", "add", "origin", f"https://github.com/{repository}.git"])
    run(["git", "-C", destination, "fetch", "--quiet", "--depth=1", "origin", sha])
    run(["git", "-C", destination, "checkout", "--quiet", "--detach", sha])
    actual = subprocess.check_output(["git", "-C", destination, "rev-parse", "HEAD"], text=True).strip()
    artifacts.require(actual == sha, "materialized source differs from the admitted revision")


def source_owners(plan, arch):
    lane = plan["lanes"][arch]
    owners = set(plan["test_overlays"])
    compiled = {repository.split("/")[1] for records in plan["product_sources"].values() for repository in records}
    if lane.get("embedded_products"):
        compiled.add("guest-runtime")
    for name, owner in artifacts.planned_helpers(lane["profile"]).items():
        if name in ("usage-probe", "cgroup-fork-probe"):
            owners.add(owner)  # standalone test helper; no sibling library checkout
        elif owner != "framework":
            compiled.add(owner)
    pending = list(compiled)
    while pending:
        owner = pending.pop()
        for dependency in LIBRARIES.get(owner, ()):
            if dependency not in compiled:
                compiled.add(dependency)
                pending.append(dependency)
    return owners | compiled


def build_records(plan, arch):
    records = dict(plan["sources"])
    products = {repository.split("/")[1] for inputs in plan["product_sources"].values() for repository in inputs}
    helpers = set(artifacts.planned_helpers(plan["lanes"][arch]["profile"]).values()) - {"framework"}
    for owner in helpers - products:
        records[owner] = plan["test_revisions"][owner]
    for owner in plan["test_overlays"]:
        artifacts.require(records[owner]["sha"] == plan["test_revisions"][owner]["sha"],
                          "candidate test checkout differs from its pin")
    return records


def checkout_records(records, root):
    root.mkdir(parents=True, exist_ok=False)
    for owner, record in sorted(records.items()):
        checkout(record["repository"], record["sha"], root / owner)
    modules = ["./" + owner for owner in sorted(records) if (root / owner / "go.mod").is_file()]
    if modules:
        # A separate test-helper checkout can sit below the product workspace.
        # Initialize its own file instead of rediscovering the parent's go.work.
        run(["go", "work", "init", *modules], cwd=root, environment={**os.environ, "GOWORK": "off"})


def materialize(plan, arch, root):
    records = build_records(plan, arch)
    checkout_records({owner: records[owner] for owner in source_owners(plan, arch)}, root)


def helper_sources(plan, arch, sources):
    owners = set(artifacts.planned_helpers(plan["lanes"][arch]["profile"]).values()) - {"framework"}
    records = build_records(plan, arch)
    if all(records[owner]["sha"] == plan["test_revisions"][owner]["sha"] for owner in owners):
        return sources
    # A linked product can still require the baseline source while its test
    # helper has an independently newer pin. Keep those compiler inputs apart.
    required = set(owners)
    pending = list(owners)
    while pending:
        for dependency in LIBRARIES.get(pending.pop(), ()):
            if dependency not in required:
                required.add(dependency)
                pending.append(dependency)
    records.update({owner: plan["test_revisions"][owner] for owner in owners})
    root = sources / "test-helpers"
    checkout_records({owner: records[owner] for owner in required}, root)
    return root


def baseline_tree(plan, arch, assets, output):
    output.mkdir()
    seen = {}
    for unit, record in plan["baseline"]["units"].items():
        name = artifacts.archive_name(unit, record["version"], arch)
        expected = next(item for item in plan["baseline"]["assets"] if item["name"] == name)
        artifacts.require("sha256:" + artifacts.digest(assets / name) == expected["digest"], "build baseline bytes differ from plan")
        artifacts.unpack(assets / name, output, unit, seen)


def build_orchestrator_cli_tests(sources, arch, output, environment):
    # Compile the owner tests from helper_sources' exact test pin. They consume
    # the independently selected product bytes only in the prepared E2E job.
    selected = {**environment, "GOWORK": "off", "GOOS": "linux", "CGO_ENABLED": "0",
                "GOARCH": {"x86_64": "amd64", "aarch64": "arm64"}[arch]}
    run(["go", "test", "-c", "-trimpath", "-o", output / "orch-cli.test", "./internal/orch"],
        cwd=sources / "orchestrator", environment=selected)


def build(plan, arch, assets, sources, output):
    artifacts.check_plan(plan)
    artifacts.require(platform.machine() == "x86_64", "both product builds run in independent x86 workspaces")
    artifacts.require(not output.exists(), "delta output already exists")
    environment = os.environ.copy()
    # This stage has no publisher or App credentials. Fail if a caller tries to
    # collapse the credentialed resolver/publisher into candidate execution.
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "CALLER_TOKEN", "KUASAR_CI_APP_PRIVATE_KEY"):
        artifacts.require(not environment.get(key), f"candidate build must not receive {key}")
    materialize(plan, arch, sources)
    (output / "bin").mkdir(parents=True)
    actual_framework = subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"], text=True).strip()
    artifacts.require(actual_framework == plan["framework_sha"], "framework checkout differs from admitted revision")
    framework_tests = output / "framework-tests"
    framework_tests.mkdir()
    shutil.copy2(ROOT / "test/e2e/e2e", framework_tests / "e2e")
    shutil.copytree(ROOT / "test/e2e/lib", framework_tests / "lib")
    environment.update(TARGET_ARCH=arch, KUASAR_WORKSPACE_ROOT=str(sources))
    lane = plan["lanes"][arch]
    kernel_root = sources
    if "vmlinux" in lane["products"] and plan["kernel_sha"] != plan["sources"]["guest-runtime"]["sha"]:
        kernel_root = sources / "kernel-unit"
        kernel_root.mkdir()
        checkout("kuasar-sandbox/guest-runtime", plan["kernel_sha"], kernel_root / "guest-runtime")
    native = {NATIVE[name] for name in lane["products"] if name in NATIVE}
    if lane.get("embedded_products"):
        native.add("envd")
    for component in sorted(native):
        selected = dict(environment)
        if component == "vmlinux":
            selected["KUASAR_WORKSPACE_ROOT"] = str(kernel_root)
        run([ROOT / "ci/native-cache/native-cache.sh", "restore-or-build", component], environment=selected)
    for name in lane["products"]:
        if name in ("mkfs.erofs", "cloud-hypervisor", "vmlinux", "sandbox-runtime.bundle"):
            continue
        owner = "guest-runtime" if artifacts.PRODUCTS[name] == "runtime" else artifacts.PRODUCTS[name]
        run(["make", "-C", sources / owner, f"TARGET_ARCH={arch}", name], environment=environment)
    for name in lane["products"]:
        if name == "sandbox-runtime.bundle":
            continue
        unit = artifacts.PRODUCTS[name]
        if name == "vmlinux":
            path = kernel_root / "guest-runtime/native-deps/bin" / arch / name
        elif name == "mkfs.erofs":
            path = sources / "guest-runtime/native-deps/bin" / arch / name
        elif name == "cloud-hypervisor":
            path = sources / "sandboxer/native-deps/bin" / arch / name
        else:
            owner = "guest-runtime" if unit == "runtime" else unit
            path = sources / owner / "bin" / arch / name
        artifacts.require(path.is_file(), f"owner recipe did not produce selected product: {name}")
        shutil.copy2(path, output / "bin" / name)
    if "envd" in lane.get("embedded_products", []):
        (output / "embedded").mkdir()
        shutil.copy2(sources / "guest-runtime/native-deps/bin" / arch / "envd", output / "embedded/envd")
    if "sandbox-runtime.bundle" in lane["products"]:
        with tempfile.TemporaryDirectory(prefix="runtime-inputs-", dir=sources) as temporary:
            work = Path(temporary)
            baseline_tree(plan, arch, assets, work / "baseline")
            payloads = work / "embedded"
            run(["python3", os.environ["KUASAR_RUNTIME_READER"], work / "baseline/bin/sandbox-runtime.bundle",
                 shutil.which("fsck.erofs"), shutil.which("dump.erofs"), payloads])
            for file in payloads.iterdir():
                if not file.name.endswith(".metadata"):
                    file.chmod(0o755)
            def selected(name, fallback):
                candidate = output / "bin" / name
                return candidate if candidate.is_file() else fallback
            init = selected("sandbox-init", payloads / "init")
            flatten = selected("flatten-ctl", work / "baseline/bin/flatten-ctl")
            mkfs = selected("mkfs.erofs", work / "baseline/bin/mkfs.erofs")
            envd = output / "embedded/envd" if "envd" in lane.get("embedded_products", []) else payloads / "envd"
            run(["make", "-C", sources / "guest-runtime", f"TARGET_ARCH={arch}", "sandbox-runtime",
                 f"SANDBOX_INIT={init}", f"ENVD={envd}", f"FLATTEN_CTL={flatten}", f"GUEST_MKFS_EROFS={mkfs}",
                 f"BUILD_MKFS_EROFS={shutil.which('mkfs.erofs')}"], environment=environment)
            shutil.copy2(sources / "guest-runtime/bin" / arch / "sandbox-runtime.bundle", output / "bin/sandbox-runtime.bundle")
    for owner in plan["test_overlays"]:
        destination = output / artifacts.test_overlay_root(owner)
        if owner == "platform":
            source = sources / owner / "test"
            for name in artifacts.tree_files(source):
                if artifacts.platform_test_path(name):
                    target = destination / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source / name, target)
        else:
            shutil.copytree(sources / owner / "test/e2e", destination)
    helpers = artifacts.planned_helpers(lane["profile"])
    if helpers:
        helper_root = output / "helpers"
        helper_root.mkdir()
        helper_source = helper_sources(plan, arch, sources)
        helper_environment = {**environment, "KUASAR_WORKSPACE_ROOT": str(helper_source)}
        if "versitygw" in helpers:
            run(["bash", ROOT / "ci/hosted/exact-assets-tools.sh"], environment={**environment, "KUASAR_E2E_TOOL_OUTPUT": str(helper_root)})
        elif "zot" in helpers:
            run(["bash", ROOT / "ci/integration/ensure-zot.sh"], environment={**environment, "BINDIR": str(helper_root)})
        if "custom-proxy" in helpers:
            run(["bash", helper_source / "orchestrator/scripts/ci-e2e-build.sh", "fixtures", arch, helper_root], environment=helper_environment)
        if "orch-cli.test" in helpers:
            build_orchestrator_cli_tests(helper_source, arch, helper_root, helper_environment)
        if "usage-probe" in helpers:
            run(["make", "-C", helper_source / "sandboxer", f"TARGET_ARCH={arch}",
                 f"E2E_FIXTURE_DIR={helper_root}", "e2e-usage-probe"], environment=helper_environment)
        if "cgroup-fork-probe" in helpers:
            run(["make", "-C", helper_source / "sandboxer", f"TARGET_ARCH={arch}",
                 f"E2E_FIXTURE_DIR={helper_root}", "e2e-cgroup-fork-probe"], environment=helper_environment)
    # Match the existing release packagers' executable/data modes independently
    # of the caller's umask. Tar transport preserves these through Actions.
    for name in artifacts.tree_files(output):
        file = output / name
        executable = file.stat().st_mode & 0o111
        file.chmod(0o755 if executable else 0o644)
    metadata = {"plan_id": artifacts.identity(plan), "arch": arch, "products": {}, "embedded": {}, "tests": {}, "helpers": {},
                "framework_tests": artifacts.tree_files(output / "framework-tests"),
                "test_revisions": plan["test_revisions"],
                "build_context": {"host": platform.machine(), "target": arch, "tools": {}, "native_inputs": {}}}
    for name in lane["products"]:
        metadata["products"][name] = {"sha256": artifacts.digest(output / "bin" / name), "sources": plan["product_sources"][name]}
    for name in lane.get("embedded_products", []):
        metadata["embedded"][name] = {"sha256": artifacts.digest(output / "embedded" / name), "sources": plan["embedded_sources"][name]}
    for owner in plan["test_overlays"]:
        metadata["tests"][owner] = artifacts.tree_files(output / artifacts.test_overlay_root(owner))
    for name, owner in helpers.items():
        revision = plan["framework_sha"] if owner == "framework" else plan["test_revisions"][owner]["sha"]
        metadata["helpers"][name] = {"sha256": artifacts.digest(output / "helpers" / name), "source_sha": revision}
    for label, command in {"go": ["go", "version"], "go-context": ["go", "env", "GOHOSTOS", "GOHOSTARCH", "GOVERSION"],
                           "cc": ["aarch64-linux-gnu-gcc" if arch == "aarch64" else environment.get("CC", "gcc"), "--version"],
                           "rustc": ["rustc", "-vV"]}.items():
        if shutil.which(command[0]):
            metadata["build_context"]["tools"][label] = subprocess.check_output(command, text=True, env=environment, cwd=sources).strip()
    metadata["build_context"]["go_payloads"] = {
        name: subprocess.check_output(["go", "version", "-m", str(output / "bin" / name)], text=True, env=environment, cwd=sources)
        for name in lane["products"] if name in artifacts.GO_PRODUCTS}
    if "envd" in lane.get("embedded_products", []):
        metadata["build_context"]["go_payloads"]["embedded/envd"] = subprocess.check_output(
            ["go", "version", "-m", str(output / "embedded/envd")], text=True, env=environment, cwd=sources)
    for component in native:
        selected = dict(environment)
        if component == "vmlinux":
            selected["KUASAR_WORKSPACE_ROOT"] = str(kernel_root)
        metadata["build_context"]["native_inputs"][component] = subprocess.check_output(
            [str(ROOT / "ci/native-cache/native-cache.sh"), "key", component], text=True, env=selected).strip()
    (output / "outputs.json").write_bytes(artifacts.canonical(metadata) + b"\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--arch", required=True, choices=artifacts.ARCHES)
    parser.add_argument("--assets", required=True, type=Path)
    parser.add_argument("--sources", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    build(json.loads(args.plan.read_text()), args.arch, args.assets.resolve(), args.sources.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()
