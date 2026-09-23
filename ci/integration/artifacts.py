#!/usr/bin/env python3
"""Trusted product selection and composition for the existing integration lanes.

The resolver supplies the immutable plan. Build outputs are untrusted data: they
can replace only planned products and complete, selected test-owner directories.
No command in this module builds or executes an extracted product.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
ARCHES = ("x86_64", "aarch64")
OWNERS = ("accelerator", "connector", "guest-runtime", "sandboxer", "orchestrator", "platform")
UNITS = ("accelerator", "connector", "sandboxer", "orchestrator", "runtime", "vmlinux")
SUITES = frozenset(("basic", "storage", "image", "network", "sandbox", "snapshot", "orchestrator", "builder", "telemetry"))
PRODUCTS = {
    "manifest-ctl": "accelerator", "store-ctl": "accelerator", "cache-ctl": "accelerator",
    "connector-ctl": "connector", "sandbox-ctl": "sandboxer", "sandbox-init": "sandboxer",
    "cloud-hypervisor": "sandboxer", "node-ctl": "orchestrator", "cluster-ctl": "orchestrator",
    "node-stub-ctl": "orchestrator", "e2b-key-ctl": "orchestrator",
    "flatten-ctl": "runtime", "mkfs.erofs": "runtime", "sandbox-runtime.bundle": "runtime",
    "vmlinux": "vmlinux",
}
GO_PRODUCTS = set(PRODUCTS) - {"cloud-hypervisor", "mkfs.erofs", "sandbox-runtime.bundle", "vmlinux"}
REQUIRED = {
    "connector": {"connector-ctl"},
    "guest-runtime": {"mkfs.erofs", "store-ctl", "flatten-ctl"},
    "accelerator": {"mkfs.erofs", "manifest-ctl", "store-ctl", "cache-ctl", "flatten-ctl"},
    "sandboxer": set(PRODUCTS) - {"node-ctl", "cluster-ctl", "node-stub-ctl", "e2b-key-ctl"},
    "orchestrator": set(PRODUCTS), "platform": set(PRODUCTS),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def identity(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def digest(path):
    with Path(path).open("rb") as data:
        return hashlib.file_digest(data, "sha256").hexdigest()


def relative(value):
    path = PurePosixPath(value)
    require(value and not path.is_absolute() and ".." not in path.parts
            and str(path) == value and "\\" not in value and not any(ord(c) < 32 for c in value),
            f"unsafe artifact path: {value!r}")
    return path


def case_name(value):
    """Validate the public case-ID contract without introducing test metadata."""
    require(isinstance(value, str) and PurePosixPath(value).name == value and value.count(".") >= 2
            and value.endswith(".sh") and ".." not in value,
            f"invalid E2E case filename: {value!r}")
    suite, rest = value.split(".", 1)
    require(suite in SUITES, f"unsupported E2E suite in case filename: {value}")
    require(re.fullmatch(r"[a-z0-9][a-z0-9._-]*\.sh", rest) is not None,
            f"invalid E2E case filename: {value!r}")
    return value


def changed_products(changes):
    """Small product closure; source libraries do not imply standalone rebuilds.

    Each caller runs its owner cases. Linked flatten and embedded init/flatten
    are explicit products; native inputs select only their own native outputs.
    Unchanged sibling CLIs are supplied by the same aggregate baseline.
    """
    products = set()
    for owner, paths in changes.items():
        require(owner in OWNERS, f"unknown candidate owner: {owner}")
        paths = [str(relative(path)) for path in paths if not path.endswith("_test.go")]
        def touches(*prefixes):
            return any(path == prefix or path.startswith(prefix.rstrip("/") + "/")
                       for path in paths for prefix in prefixes)
        go = touches("go.mod", "go.sum", "Makefile", "cmd", "pkg", "internal", "api", "proto")
        if owner == "accelerator":
            if go or touches("deps"):
                products.update(("manifest-ctl", "store-ctl", "cache-ctl"))
            if touches("go.mod", "go.sum", "pkg/flatten", "pkg/image", "pkg/manifest",
                       "pkg/remote", "pkg/sparse", "pkg/tailzip", "pkg/tarstream", "pkg/tar",
                       "pkg/cache", "pkg/store", "pkg/readerr", "internal/util"):
                products.add("flatten-ctl")
        elif owner == "connector" and go:
            products.add("connector-ctl")
        elif owner == "sandboxer":
            if go:
                products.add("sandbox-ctl")
            if touches("go.mod", "go.sum", "Makefile", "cmd/sandbox-init", "internal", "pkg"):
                products.add("sandbox-init")
            if touches("native-deps"):
                products.add("cloud-hypervisor")
        elif owner == "orchestrator" and (go or touches("app", "config")):
            products.update(name for name, unit in PRODUCTS.items() if unit == "orchestrator")
        elif owner == "guest-runtime":
            if go:
                products.add("flatten-ctl")
            if touches("Makefile", "cmd/runtime-bundle", "runtime", "native-deps/Makefile",
                       "native-deps/deps/common.sh", "native-deps/deps/build-envd.sh"):
                products.add("sandbox-runtime.bundle")
            if touches("native-deps/Makefile", "native-deps/deps/common.sh",
                       "native-deps/deps/build-erofs.sh", "native-deps/deps/erofs-recipe.sh",
                       "native-deps/deps/erofs-patches"):
                products.add("mkfs.erofs")
            if touches("native-deps/Makefile", "native-deps/deps/common.sh",
                       "native-deps/deps/build-vmlinux.sh", "native-deps/deps/vmlinux",
                       "native-deps/deps/linux-patches"):
                products.add("vmlinux")
    if products & {"sandbox-init", "flatten-ctl", "mkfs.erofs"}:
        products.add("sandbox-runtime.bundle")
    return sorted(products)


def profiles(owners, arch, candidate_cases=None):
    """Build the transitional internal shard profile from exact case filenames.

    Owner profiles remain an internal CI scheduling detail until the final suite
    cutover. Once an admitted candidate supplies rewritten cases, those cases are
    the executable entries; a deleted owner run_all.sh is never synthesized.
    """
    candidate_cases = candidate_cases or {}
    require(arch in ARCHES and set(owners) <= set(OWNERS), "invalid profile request")
    require(isinstance(candidate_cases, dict) and set(candidate_cases) <= set(owners),
            "candidate case ownership differs from selected owners")
    for owner, names in candidate_cases.items():
        require(isinstance(names, list) and names and names == sorted(set(names)),
                f"invalid candidate case list for {owner}")
        for name in names:
            case_name(name)
    selected = set(OWNERS if "platform" in owners else owners)
    exclusions = []
    if "accelerator" in selected:
        accelerator_cases = candidate_cases.get("accelerator", [])
        if "storage.obs.sh" in accelerator_cases:
            exclusions.append({"owner": "accelerator", "case": "storage.obs.sh", "reason": "credentialed OBS case is not selected"})
        elif not accelerator_cases:
            exclusions.append({"owner": "accelerator", "case": "e2e_obs.sh", "reason": "credentialed OBS suite is not selected"})
    if arch == "aarch64":
        for owner in sorted(selected - {"accelerator", "guest-runtime"}):
            exclusions.append({"owner": owner, "reason": "no selected independent ARM non-KVM owner suite"})
        selected &= {"accelerator", "guest-runtime"}
    cases = []
    for owner in OWNERS:
        if owner not in selected:
            continue
        names = candidate_cases.get(owner, [])
        if names:
            cases.extend(f"test/e2e/{owner}/cases/{name}" for name in names
                         if not (owner == "accelerator" and name == "storage.obs.sh"))
        else:
            cases.append(f"test/e2e/{owner}/run_all.sh")
    required = set().union(*(REQUIRED[owner] for owner in selected))
    return {"cases": cases, "required_products": sorted(required), "exclusions": exclusions,
            "name": "x86-owner-kvm" if arch == "x86_64" else ("arm-native-non-kvm" if cases else "arm-static-only")}


def shards(profile):
    result = {}
    for case in profile["cases"]:
        owner = PurePosixPath(case).parts[2]
        shard = owner if owner in ("sandboxer", "orchestrator") else "core"
        result.setdefault(shard, []).append(case)
    return result or {"static": []}


def planned_helpers(profile):
    owners = {PurePosixPath(case).parts[2] for case in profile["cases"]}
    helpers = {}
    if owners & {"guest-runtime", "sandboxer", "orchestrator", "platform"}:
        helpers["zot"] = "framework"
    if owners & {"orchestrator", "platform"}:
        helpers["versitygw"] = "framework"
    if "orchestrator" in owners:
        helpers.update({"custom-proxy": "orchestrator", "telemetry-grpc-probe": "orchestrator",
                        "orch-cli.test": "orchestrator"})
    if "sandboxer" in owners:
        helpers["usage-probe"] = "sandboxer"
    return helpers


def archive_name(unit, version, arch):
    require(unit in UNITS and arch in ARCHES, "invalid archive unit/architecture")
    prefix = unit + "-" if unit in ("runtime", "vmlinux") else ""
    require(re.fullmatch(re.escape(prefix) + r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-preview\.[0-9]{8}(?:\.[1-9][0-9]*)?)?", version),
            "invalid unit version")
    if unit == "runtime":
        return f"sandbox-runtime-{arch}-{version.removeprefix('runtime-')}.tar.gz"
    if unit == "vmlinux":
        return f"vmlinux-{arch}-{version.removeprefix('vmlinux-')}.tar.gz"
    return f"{unit}-{version}-linux-{arch}.tar.gz"


def check_architecture(path, arch, *, kernel=False):
    require(arch in ARCHES, "invalid target architecture")
    with Path(path).open("rb") as data:
        header = data.read(64)
    if kernel and arch == "aarch64":
        require(len(header) == 64 and header[56:60] == b"ARM\x64", "expected ARM kernel Image")
        size, flags = struct.unpack_from("<QQ", header, 16)
        require(size >= 64 and flags & 1 == 0 and not any(header[32:56]), "invalid ARM Image header")
    else:
        require(len(header) == 64 and header[:7] == b"\x7fELF\x02\x01\x01", "expected little-endian ELF64")
        require(struct.unpack_from("<H", header, 18)[0] == {"x86_64": 62, "aarch64": 183}[arch],
                f"wrong ELF architecture for {Path(path).name}: expected {arch}")


def tree_files(root):
    result = {}
    require(root.is_dir() and not root.is_symlink(), f"missing artifact directory: {root}")
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), f"symlink in artifact: {path}")
        if path.is_dir():
            continue
        require(path.is_file(), f"non-regular artifact: {path}")
        result[str(path.relative_to(root))] = digest(path)
    return result


def tree_modes(root):
    return {name: (root / name).stat().st_mode & 0o7777 for name in tree_files(root)}


def test_overlay_root(owner):
    return "test/platform" if owner == "platform" else f"test/e2e/{owner}"


def overlay_case_path(owner, name):
    case_name(name)
    return f"e2e/platform/cases/{name}" if owner == "platform" else f"cases/{name}"


def platform_test_path(path):
    relative(path)
    return (not path.startswith("scripts/")
            and not any(path.startswith(f"e2e/{owner}/") for owner in OWNERS if owner != "platform")
            and path not in ("e2e/assemble.sh", "e2e/assemble_docs.py"))


def normalize_e2e_cases(stage):
    """Expose owner cases/libs through the one public prepared-runner layout."""
    root = stage / "test/e2e"
    cases = root / "cases"
    if cases.exists():
        shutil.rmtree(cases)
    cases.mkdir()
    seen = {}
    for owner in OWNERS:
        suite = root / owner
        owner_cases = suite / "cases"
        if owner_cases.is_dir():
            for source in sorted(owner_cases.iterdir()):
                require(source.is_file() and not source.is_symlink(), f"non-file in {owner} E2E cases: {source.name}")
                name = case_name(source.name)
                require(source.stat().st_mode & 0o111, f"non-executable E2E case: {owner}/{name}")
                require(name not in seen, f"duplicate E2E case ID: {name} ({seen.get(name)} and {owner})")
                seen[name] = owner
                shutil.copy2(source, cases / name)
        owner_lib = suite / "lib"
        if owner_lib.is_dir():
            target = root / "lib" / owner
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(owner_lib, target)
    return seen


def runtime_payloads(workspace, arch, expected_init=None, expected_envd=None):
    reader = Path(os.environ["KUASAR_RUNTIME_READER"])
    require(digest(reader) == "01d97e1cc7aed550305e13b4dfd1daa95a3740be84512d195aed46a277e661fd",
            "untrusted Runtime reader")
    fsck, dump = shutil.which("fsck.erofs"), shutil.which("dump.erofs")
    require(fsck and dump, "trusted host EROFS readers are required")
    with tempfile.TemporaryDirectory(prefix="runtime-identity-") as temporary:
        payloads = Path(temporary) / "payloads"
        subprocess.run(["python3", str(reader), str(workspace / "bin/sandbox-runtime.bundle"),
                        fsck, dump, str(payloads)], check=True)
        result = {}
        for name in ("init", "envd", "flatten-ctl", "mkfs.erofs"):
            path = payloads / name
            check_architecture(path, arch)
            result[name] = digest(path)
            if name in ("flatten-ctl", "mkfs.erofs"):
                require(result[name] == digest(workspace / "bin" / name),
                        f"embedded {name} differs from the selected product")
        for name, expected in (("init", expected_init), ("envd", expected_envd)):
            if expected is not None:
                require(result[name] == expected, f"embedded {name} differs from the selected product")
        return result


def unpack(archive, destination, unit, seen):
    """Extract regular files only, with canonical names and fixed ownership."""
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        local = set()
        for member in members:
            name = member.name.removeprefix("./").rstrip("/")
            if name in ("", ".") and member.isdir():
                continue
            relative(name)
            require(name not in local, f"duplicate archive member: {name}")
            local.add(name)
            require((member.isfile() or member.isdir()) and member.uid == member.gid == 0
                    and member.mode & 0o7022 == 0, f"unsafe archive entry: {name}")
            if member.isdir():
                continue
            require(name not in seen, f"conflicting archive ownership: {name}")
            if unit == "platform":
                require(name.startswith(("test/", "docs/")), f"platform cannot own {name}")
            elif name.startswith("bin/"):
                require(PRODUCTS.get(name[4:]) == unit, f"{unit} cannot own {name}")
            elif name.startswith("share/"):
                require(name.startswith((f"share/licenses/{unit}/", f"share/sources/{unit}/")),
                        f"{unit} cannot own {name}")
            else:
                require((unit == "accelerator" and name.startswith("test/scripts/"))
                        or (unit in ("connector", "orchestrator") and name.startswith(("deploy/", "test/"))),
                        f"undeclared {unit} payload: {name}")
            seen[name] = unit
        for member in members:
            name = member.name.removeprefix("./").rstrip("/")
            if not member.isfile():
                continue
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as data, target.open("xb") as output:
                shutil.copyfileobj(data, output)
            target.chmod(member.mode & 0o777)


def validate_test_revisions(records):
    require(isinstance(records, dict) and set(records) == set(OWNERS), "missing or unexpected owner test pins")
    for owner, record in records.items():
        repository = "kuasar-sandbox/" + ("kuasar-sandbox" if owner == "platform" else owner)
        require(isinstance(record, dict) and set(record) == {"repository", "sha", "role"}
                and record["repository"] == repository
                and isinstance(record["sha"], str) and re.fullmatch(r"[0-9a-f]{40}", record["sha"])
                and record["role"] in ("release", "baseline", "candidate", "companion"), "invalid owner test pin")
    return records


def release_test_revisions(pins, platform_sha):
    require(isinstance(pins, dict) and set(pins) == set(OWNERS) - {"platform"}, "missing release owner test pins")
    records = {owner: {"repository": "kuasar-sandbox/" + ("kuasar-sandbox" if owner == "platform" else owner),
                       "sha": platform_sha if owner == "platform" else pins[owner], "role": "release"}
               for owner in OWNERS}
    return validate_test_revisions(records)


def check_plan(plan):
    require(plan["schema"] == 1, "unsupported integration plan")
    require(re.fullmatch(r"[0-9a-f]{40}", plan["framework_sha"]), "missing exact framework revision")
    require(set(plan["lanes"]) == set(ARCHES), "plan must contain both architectures")
    candidate_cases = plan.get("candidate_cases", {})
    require(isinstance(candidate_cases, dict) and set(candidate_cases) <= set(plan.get("test_overlays", [])),
            "candidate case ownership differs from test overlays")
    for owner, names in candidate_cases.items():
        require(owner in OWNERS and isinstance(names, list) and names and names == sorted(set(names)),
                f"invalid candidate case list for {owner}")
        for name in names:
            case_name(name)
    for arch, lane in plan["lanes"].items():
        require(lane["products"] == sorted(set(lane["products"])) and set(lane["products"]) <= set(PRODUCTS),
                "invalid affected product set")
        require(lane.get("embedded_products", []) in ([], ["envd"]), "unknown embedded product")
        require(lane["profile"] == profiles(plan["owners"], arch, candidate_cases), "profile differs from trusted owner selection")
        expected_extra = {"sandboxer": ["working-set-smoke"]} if plan.get("mode") == "source" and arch == "x86_64" and set(plan["owners"]) & {"platform", "sandboxer"} else {}
        require(lane.get("extra_checks", {}) == expected_extra, "extra checks differ from trusted mode/owner selection")
    validate_test_revisions(plan.get("test_revisions"))
    return identity(plan)


def compose(plan, arch, assets, delta, output):
    plan_id = check_plan(plan)
    require(arch in ARCHES and output.name == arch and not output.exists(),
            "prepare needs a fresh, architecture-named destination")
    lane = plan["lanes"][arch]
    baseline = plan["baseline"]
    candidate_cases = plan.get("candidate_cases", {})
    expected_assets = {archive_name(unit, record["version"], arch): unit
                       for unit, record in baseline["units"].items()}
    require(set(baseline["units"]) == set(UNITS), "incomplete aggregate unit selection")
    expected_assets[f"platform-{baseline['version']}.tar.gz"] = "platform"
    records = {record["name"]: record for record in baseline["assets"]}
    require(len(records) == len(baseline["assets"]) and set(expected_assets) <= set(records),
            "aggregate lacks target assets; explicit ARM initialization is required")
    for name in expected_assets:
        path = assets / name
        require(path.is_file() and not path.is_symlink() and path.stat().st_size == records[name]["size"]
                and "sha256:" + digest(path) == records[name]["digest"], f"baseline asset digest mismatch: {name}")
    metadata = json.loads((delta / "outputs.json").read_text())
    require(metadata["plan_id"] == plan_id and metadata["arch"] == arch, "candidate input identity mismatch")
    require(metadata.get("test_revisions") == plan["test_revisions"], "build test pins differ from plan")
    require(set(metadata["products"]) == set(lane["products"]), "candidate product ownership differs from plan")
    require(set(metadata["tests"]) == set(plan["test_overlays"]), "candidate test ownership differs from plan")
    require(set(metadata.get("embedded", {})) == set(lane.get("embedded_products", [])),
            "candidate embedded ownership differs from plan")
    helpers = planned_helpers(lane["profile"])
    require(set(metadata.get("helpers", {})) == set(helpers), "test helper selection differs from plan")
    framework_tests = metadata.get("framework_tests", {})
    require(isinstance(framework_tests, dict) and "e2e" in framework_tests and "lib/common.sh" in framework_tests,
            "candidate is missing trusted framework E2E files")
    require(tree_files(delta / "framework-tests") == framework_tests, "framework E2E file digest mismatch")
    expected_delta = {"outputs.json"} | {f"framework-tests/{name}" for name in framework_tests}
    for name, record in metadata["products"].items():
        file = delta / "bin" / name
        require(file.is_file() and not file.is_symlink() and digest(file) == record["sha256"],
                f"candidate digest mismatch: {name}")
        require(record["sources"] == plan["product_sources"][name], f"candidate source identity mismatch: {name}")
        expected_delta.add("bin/" + name)
    for name, record in metadata.get("embedded", {}).items():
        file = delta / "embedded" / name
        require(file.is_file() and not file.is_symlink() and digest(file) == record["sha256"],
                f"candidate embedded digest mismatch: {name}")
        require(record["sources"] == plan["embedded_sources"][name], "embedded source identity mismatch")
        check_architecture(file, arch)
        expected_delta.add("embedded/" + name)
    for name, owner in helpers.items():
        file = delta / "helpers" / name
        record = metadata["helpers"][name]
        revision = plan["framework_sha"] if owner == "framework" else plan["test_revisions"][owner]["sha"]
        require(file.is_file() and not file.is_symlink() and digest(file) == record["sha256"]
                and record["source_sha"] == revision, f"test helper identity mismatch: {name}")
        check_architecture(file, arch)
        expected_delta.add("helpers/" + name)
    for owner, files in metadata["tests"].items():
        directory = test_overlay_root(owner)
        root = delta / directory
        require(owner in OWNERS and tree_files(root) == files,
                f"candidate test directory digest mismatch: {owner}")
        names = candidate_cases.get(owner, [])
        if names:
            modes = tree_modes(root)
            for name in names:
                entry = overlay_case_path(owner, name)
                require(entry in files and modes[entry] & 0o111, f"missing executable candidate case: {owner}/{name}")
        else:
            entry = "e2e/platform/run_all.sh" if owner == "platform" else "run_all.sh"
            require(entry in files, f"missing candidate owner entry: {owner}")
        if owner == "platform":
            require(all(platform_test_path(name) for name in files), "platform cannot replace another test owner")
        expected_delta.update(f"{directory}/{name}" for name in files)
    require(set(tree_files(delta)) == expected_delta, "undeclared candidate overlay files")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{arch}.", dir=output.parent) as temporary:
        stage = Path(temporary) / arch
        stage.mkdir()
        ownership = {}
        for name, unit in expected_assets.items():
            unpack(assets / name, stage, unit, ownership)
        originals = {name: digest(stage / "bin" / name) for name in PRODUCTS}
        original_embedded = runtime_payloads(stage, arch)
        for name in lane["products"]:
            shutil.copy2(delta / "bin" / name, stage / "bin" / name)
        for owner in plan["test_overlays"]:
            source = delta / test_overlay_root(owner)
            if owner == "platform":
                for path in list((stage / "test").rglob("*")):
                    relative_path = str(path.relative_to(stage / "test"))
                    if path.is_file() and platform_test_path(relative_path):
                        path.unlink()
                shutil.copytree(source, stage / "test", dirs_exist_ok=True)
            else:
                destination = stage / "test/e2e" / owner
                shutil.rmtree(destination)
                shutil.copytree(source, destination)
        shutil.copytree(delta / "framework-tests", stage / "test/e2e", dirs_exist_ok=True)
        normalized = normalize_e2e_cases(stage)
        for owner, names in candidate_cases.items():
            for name in names:
                require(normalized.get(name) == owner, f"prepared candidate case ownership changed: {name}")
        products = {}
        for name, unit in PRODUCTS.items():
            path = stage / "bin" / name
            require(path.is_file(), f"missing final product: {name}")
            if name not in ("vmlinux", "sandbox-runtime.bundle"):
                require(path.stat().st_mode & 0o111 and path.stat().st_mode & 0o7022 == 0,
                        f"selected product has unsafe or non-executable permissions: {name}")
            if name != "sandbox-runtime.bundle":
                check_architecture(path, arch, kernel=name == "vmlinux")
            actual = digest(path)
            candidate = name in lane["products"]
            require(actual == (metadata["products"][name]["sha256"] if candidate else originals[name]),
                    f"final product bytes differ from their origin: {name}")
            products[name] = {"sha256": actual, "unit": unit, "origin": "candidate" if candidate else "baseline",
                              "sources": plan["product_sources"][name] if candidate else baseline["units"][unit]}
        for case in lane["profile"]["cases"]:
            path = stage / relative(case)
            require(path.is_file() and os.access(path, os.X_OK), f"missing selected test entry: {case}")
        for directory in ("images", "manifests", "fixtures"):
            (stage / directory).mkdir(exist_ok=True)
        if helpers:
            shutil.copytree(delta / "helpers", stage / "fixtures/bin")
        expected_init = products["sandbox-init"]["sha256"] if "sandbox-init" in lane["products"] else original_embedded["init"]
        expected_envd = metadata.get("embedded", {}).get("envd", {}).get("sha256", original_embedded["envd"])
        if "sandbox-runtime.bundle" in lane["products"]:
            embedded = runtime_payloads(stage, arch, expected_init, expected_envd)
        else:
            embedded = original_embedded
            require(embedded["init"] == expected_init and embedded["envd"] == expected_envd,
                    "embedded init differs from the selected product")
            for name in ("flatten-ctl", "mkfs.erofs"):
                require(embedded[name] == products[name]["sha256"], f"embedded {name} differs from the selected product")
        provenance = {"schema": 1, "plan_id": plan_id, "arch": arch, "framework_sha": plan["framework_sha"],
                      "baseline": baseline, "products": products, "test_revisions": plan["test_revisions"],
                      "build_context": metadata["build_context"], "profile": lane["profile"],
                      "embedded": embedded, "helpers": metadata.get("helpers", {}),
                      "files": tree_files(stage), "modes": tree_modes(stage)}
        (stage / "provenance.json").write_bytes(canonical(provenance) + b"\n")
        stage.rename(output)
    return provenance


def verify_workspace(workspace, plan, arch):
    provenance = json.loads((workspace / "provenance.json").read_text())
    require(provenance["plan_id"] == check_plan(plan) and provenance["arch"] == arch,
            "prepared workspace has the wrong plan/architecture")
    actual = tree_files(workspace)
    del actual["provenance.json"]
    require(actual == provenance["files"], "prepared workspace changed after composition")
    modes = tree_modes(workspace)
    del modes["provenance.json"]
    require(modes == provenance["modes"], "prepared workspace permissions changed")
    require(provenance["profile"] == plan["lanes"][arch]["profile"], "selected profile changed after preparation")
    require(provenance.get("test_revisions") == plan["test_revisions"], "prepared test pins differ from plan")
    return provenance


def collect_results(plan, results):
    require(set(results) == set(ARCHES), "both architecture results are required")
    for arch in ARCHES:
        record = results[arch]
        require(record["plan_id"] == check_plan(plan) and record["arch"] == arch,
                "result belongs to a different plan or architecture")
        require(record["conclusion"] == "success" and record["profile"] == plan["lanes"][arch]["profile"],
                f"selected {arch} validation did not pass")
        require(record.get("test_revisions") == plan["test_revisions"], "result test pins differ from plan")
    return {"plan_id": identity(plan), "test_revisions": plan["test_revisions"], "architectures": results}


def collect_shard_results(plan, arch, results):
    expected = shards(plan["lanes"][arch]["profile"])
    require(set(results) == set(expected), f"missing or unexpected {arch} shard results")
    prepared = set()
    for shard, cases in expected.items():
        result = results[shard]
        require(result["arch"] == arch and result["shard"] == shard and result["plan_id"] == check_plan(plan),
                "shard result has the wrong immutable input identity")
        require(result["conclusion"] == "success" and result["cases"] == cases,
                "selected shard validation did not pass")
        require(result.get("extra_checks", []) == plan["lanes"][arch].get("extra_checks", {}).get(shard, []),
                "required extra checks are missing")
        require(result.get("test_revisions") == plan["test_revisions"], "shard test pins differ from plan")
        require(re.fullmatch(r"[0-9a-f]{64}", result["provenance_sha256"]), "missing prepared input identity")
        prepared.add(result["provenance_sha256"])
    require(len(prepared) == 1, "shards executed different prepared workspaces")
    return {"arch": arch, "plan_id": identity(plan), "conclusion": "success",
            "profile": plan["lanes"][arch]["profile"], "shards": results, "provenance_sha256": prepared.pop(),
            "test_revisions": plan["test_revisions"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("compose", "verify", "results", "shard-results"))
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--arch", choices=ARCHES)
    parser.add_argument("--assets", type=Path)
    parser.add_argument("--delta", type=Path)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--results", type=Path)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    if args.command == "compose":
        compose(plan, args.arch, args.assets, args.delta, args.workspace)
    elif args.command == "verify":
        verify_workspace(args.workspace, plan, args.arch)
    elif args.command == "results":
        result = collect_results(plan, {arch: json.loads((args.results / f"{arch}.json").read_text()) for arch in ARCHES})
        print(json.dumps(result, sort_keys=True))
    else:
        result = collect_shard_results(plan, args.arch, {
            path.stem: json.loads(path.read_text()) for path in args.results.glob("*.json")})
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
