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
import source_inputs
import build_helpers
import build_demo_wheels

ROOT = Path(__file__).resolve().parents[2]
LIBRARIES = {"sandboxer": ("accelerator", "connector"), "guest-runtime": ("accelerator",),
             "orchestrator": ("accelerator", "connector", "sandboxer")}
NATIVE = {"cache-ctl": "rocksdb", "mkfs.erofs": "erofs", "cloud-hypervisor": "cloud-hypervisor", "vmlinux": "vmlinux"}


def run(command, *, cwd=None, environment=None):
    subprocess.run([str(arg) for arg in command], cwd=cwd, env=environment, check=True)



def compiled_owners(plan, arch):
    lane = plan["lanes"][arch]
    compiled = {repository.split("/")[1] for records in plan["product_sources"].values() for repository in records}
    if lane.get("embedded_products"):
        compiled.add("guest-runtime")
    return dependencies(compiled)


def dependencies(owners):
    compiled = set(owners)
    pending = list(compiled)
    while pending:
        owner = pending.pop()
        for dependency in LIBRARIES.get(owner, ()):
            if dependency not in compiled:
                compiled.add(dependency)
                pending.append(dependency)
    return compiled


def source_owners(plan, arch):
    return set(plan["test_overlays"]) | compiled_owners(plan, arch)


def build_records(plan, arch):
    records = dict(plan["sources"])
    for owner in set(plan["test_overlays"]) - compiled_owners(plan, arch):
        records[owner] = plan["test_revisions"][owner]
    return records


def configure_workspace(root):
    modules = ["./" + owner.name for owner in sorted(root.iterdir())
               if owner.is_dir() and (owner / "go.mod").is_file()]
    if modules:
        # Compiler/toolchain selection runs only inside the build environment.
        run(["go", "work", "init", *modules], cwd=root, environment={**os.environ, "GOWORK": "off"})


def helper_owners(plan, arch):
    if plan["mode"] != "source":
        return set()
    return dependencies(set(artifacts.planned_helpers(plan["lanes"][arch]["selection"]).values()) - {"framework"})


def separate_helpers(plan, arch):
    records = build_records(plan, arch)
    return any(owner not in source_owners(plan, arch) or records[owner]["sha"] != plan["test_revisions"][owner]["sha"]
               for owner in helper_owners(plan, arch))


def source_layout(plan, arch):
    records = build_records(plan, arch)
    layout = {owner: records[owner] for owner in source_owners(plan, arch)}
    for owner in plan["test_overlays"]:
        if records[owner]["sha"] != plan["test_revisions"][owner]["sha"]:
            layout["test-overlays/" + owner] = plan["test_revisions"][owner]
    if separate_helpers(plan, arch):
        layout.update({"test-helpers/" + owner: plan["test_revisions"][owner] for owner in helper_owners(plan, arch)})
    if "vmlinux" in plan["lanes"][arch].get("products", []) and plan["kernel_sha"] != plan["sources"]["guest-runtime"]["sha"]:
        layout["kernel-unit/guest-runtime"] = {"repository": "kuasar-sandbox/guest-runtime", "sha": plan["kernel_sha"]}
    return layout


def materialize(plan, arch, root):
    """Fetch exact public inputs on the host without invoking any toolchain."""
    root.mkdir(parents=True, exist_ok=False)
    source_inputs.copy_run_inputs(plan, source_layout(plan, arch), root)


def verify_materialized(plan, arch, root):
    for relative, record in sorted(source_layout(plan, arch).items()):
        source = root / relative
        actual = subprocess.check_output(["git", "-C", source, "rev-parse", "HEAD"], text=True).strip()
        artifacts.require(actual == record["sha"], "materialized source identity changed: " + relative)
        clean = subprocess.run(["git", "-C", source, "diff", "--no-ext-diff", "--no-textconv", "--exit-code", "HEAD", "--"],
                               capture_output=True, text=True)
        artifacts.require(clean.returncode == 0, "materialized source has modified tracked inputs: " + relative)
        untracked = subprocess.check_output(["git", "-C", source, "ls-files", "--others", "--exclude-standard"], text=True)
        artifacts.require(not untracked, "materialized source has untracked inputs: " + relative)


def helper_sources(plan, arch, sources):
    root = sources / "test-helpers" if separate_helpers(plan, arch) else sources
    artifacts.require(root.is_dir(), "exact helper inputs were not materialized")
    return root


def test_source(plan, arch, sources, owner):
    record = plan["test_revisions"][owner]
    root = sources / owner if build_records(plan, arch)[owner]["sha"] == record["sha"] else sources / "test-overlays" / owner
    artifacts.require(root.is_dir(), "exact test overlay was not materialized: " + owner)
    return root


def baseline_tree(plan, arch, assets, output):
    output.mkdir()
    seen = {}
    for unit, record in plan["baseline"]["units"].items():
        name = artifacts.archive_name(unit, record["version"], arch)
        expected = next(item for item in plan["baseline"]["assets"] if item["name"] == name)
        artifacts.require("sha256:" + artifacts.digest(assets / name) == expected["digest"], "build baseline bytes differ from plan")
        artifacts.unpack(assets / name, output, unit, seen)


def build(plan, arch, assets, sources, output, *, materialized=False):
    artifacts.check_plan(plan)
    artifacts.require(platform.machine() == arch, "product and helper builds require the selected native architecture")
    artifacts.require(not output.exists(), "delta output already exists")
    environment = os.environ.copy()
    # This stage has no publisher or App credentials. Fail if a caller tries to
    # collapse the credentialed resolver/publisher into candidate execution.
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "CALLER_TOKEN", "KUASAR_CI_APP_PRIVATE_KEY"):
        artifacts.require(not environment.get(key), f"candidate build must not receive {key}")
    actual_framework = subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"], text=True).strip()
    artifacts.require(actual_framework == plan["framework_sha"], "framework checkout differs from admitted revision")
    if materialized:
        verify_materialized(plan, arch, sources)
    else:
        materialize(plan, arch, sources)
    configure_workspace(sources)
    if separate_helpers(plan, arch):
        configure_workspace(sources / "test-helpers")
    (output / "bin").mkdir(parents=True)
    environment.update(TARGET_ARCH=arch, KUASAR_WORKSPACE_ROOT=str(sources))
    lane = plan["lanes"][arch]
    kernel_root = sources
    if "vmlinux" in lane["products"] and plan["kernel_sha"] != plan["sources"]["guest-runtime"]["sha"]:
        kernel_root = sources / "kernel-unit"
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
        pinned = test_source(plan, arch, sources, owner)
        if owner == "platform":
            source = pinned / "test"
            for name in artifacts.tree_files(source):
                if artifacts.platform_test_path(name):
                    target = destination / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source / name, target)
        else:
            shutil.copytree(pinned / "test/e2e", destination)
    helpers = artifacts.planned_helpers(lane["selection"]) if plan["mode"] == "source" else {}
    if plan['mode'] == 'source' and 'basic.demo.sh' in lane['selection']['cases']:
        demo = output / artifacts.test_overlay_root('platform') / 'demo'
        build_demo_wheels.build(demo, arch, demo / 'wheels' / arch)
    if helpers:
        helper_source = helper_sources(plan, arch, sources)
        build_helpers.build(helper_source, arch, output / "helpers", helpers, environment)
    # Match the existing release packagers' executable/data modes independently
    # of the caller's umask. Tar transport preserves these through Actions.
    for name in artifacts.tree_files(output):
        file = output / name
        executable = file.stat().st_mode & 0o111
        file.chmod(0o755 if executable else 0o644)
    metadata = {"plan_id": artifacts.identity(plan), "arch": arch, "products": {}, "embedded": {}, "tests": {}, "helpers": {},
                "test_revisions": plan["test_revisions"],
                "build_context": {"host": platform.machine(), "target": arch, "tools": {}, "native_inputs": {}}}
    workbench = {name: environment[key] for name, key in (
        ("image_id", "KUASAR_WORKBENCH_IMAGE_ID"), ("framework_sha", "KUASAR_WORKBENCH_FRAMEWORK_SHA"))
        if key in environment}
    selection = sources / ".ci/workbench.json"
    if selection.is_file():
        workbench["selection"] = json.loads(selection.read_text())
    if workbench:
        # Diagnostic provenance only. The trusted plan and action receipt remain
        # the authority for image/source admission, independently of build output.
        metadata["build_context"]["workbench"] = workbench
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
                           "cc": [environment.get("CC", "gcc"), "--version"],
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--materialize-only", action="store_true", help="fetch exact inputs without running compilers")
    mode.add_argument("--materialized", action="store_true", help="verify and compile previously fetched exact inputs")
    args = parser.parse_args()
    source_inputs.INPUT_ARCHIVE = args.plan.resolve().parent / "source-inputs.tar"
    plan = json.loads(args.plan.read_text())
    artifacts.check_plan(plan)
    if args.materialize_only:
        materialize(plan, args.arch, args.sources.resolve())
    else:
        build(plan, args.arch, args.assets.resolve(), args.sources.resolve(), args.output.resolve(),
              materialized=args.materialized)


if __name__ == "__main__":
    main()
