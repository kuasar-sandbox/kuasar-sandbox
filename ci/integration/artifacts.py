#!/usr/bin/env python3
"""Trusted product selection and composition for the existing integration lanes.

The resolver supplies the immutable plan. Build outputs are untrusted data: they
can replace only planned products and complete, selected test-owner directories.
No command in this module builds or executes an extracted product.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]

_workspace_spec = importlib.util.spec_from_file_location(
    'public_e2e_workspace', ROOT / 'test/e2e/lib/workspace.py')
_public_workspace = importlib.util.module_from_spec(_workspace_spec)
_workspace_spec.loader.exec_module(_public_workspace)
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

    Each caller selects broad suites from its owned case filenames. Linked flatten and embedded init/flatten
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
            # These are the libraries imported by guest-runtime's flatten CLI.
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


DEFAULT_SUITES = {
    "accelerator": {"storage", "image"}, "connector": {"network"},
    "guest-runtime": {"image", "sandbox"},
    "sandboxer": {"basic", "sandbox", "snapshot", "telemetry"},
    "orchestrator": {"basic", "orchestrator", "builder", "telemetry"},
    "platform": set(SUITES),
}


def suite_selection(owners, arch, case_files):
    """Select broad suites from exact source filenames, then apply lane limits."""
    require(arch in ARCHES and set(owners) <= set(OWNERS), "invalid suite selection request")
    require(isinstance(case_files, dict) and set(case_files) == set(OWNERS), "incomplete test case source set")
    all_cases, by_owner = {}, {}
    for owner, names in case_files.items():
        require(isinstance(names, list) and names and names == sorted(set(names)), f"invalid case list for {owner}")
        by_owner[owner] = set(names)
        for name in names:
            case_name(name)
            require(name not in all_cases, f"duplicate E2E case ID: {name}")
            all_cases[name] = owner
    suites = set().union(*(DEFAULT_SUITES[owner] for owner in owners))
    # An owner's cases outside its defaults expand the selected suites too;
    # e.g. sandboxer's image and network cases must retain their CI coverage.
    suites.update(name.split('.')[0] for owner in owners for name in case_files[owner])
    chosen = {name for name in all_cases if name.split('.')[0] in suites}
    exclusions = []
    if 'storage.obs.sh' in chosen:
        chosen.remove('storage.obs.sh')
        exclusions.append({'case': 'storage.obs.sh', 'reason': 'credentialed OBS case requires explicit opt-in'})
    if arch == 'aarch64':
        supported = by_owner['accelerator'] | by_owner['guest-runtime']
        for name in sorted(chosen - supported):
            exclusions.append({'case': name, 'reason': 'outside the accepted native ARM non-KVM subset'})
        chosen &= supported
    required = set().union(*(REQUIRED[all_cases[name]] for name in chosen))
    return {'suites': sorted(suites), 'cases': sorted(chosen), 'required_products': sorted(required),
            'exclusions': exclusions}


def shards(selection):
    result = {}
    for case in selection['cases']:
        suite = case.split('.')[0]
        result.setdefault(suite, []).append(case)
    return result or {'static': []}


def planned_helpers(selection):
    return _public_workspace.required_helpers(selection['cases'])


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
    # Platform helpers/perf scripts live outside test/e2e/platform. Their whole
    # owned tree travels together, excluding the component-owned subtrees.
    return "test/platform" if owner == "platform" else f"test/e2e/{owner}"


def overlay_case_path(owner, name):
    case_name(name)
    return f"e2e/platform/cases/{name}" if owner == "platform" else f"cases/{name}"


def platform_test_path(path):
    relative(path)
    if path.startswith('e2e/'):
        return path == 'e2e/e2e' or path.startswith(('e2e/lib/', 'e2e/platform/'))
    return not path.startswith('scripts/')


def validate_e2e_layout(files, directories=()):
    """Keep runtime inputs flat while retaining non-executable owner guides."""
    guides = {name for name, mode in files.items()
              if len(Path(name).parts) > 1 and Path(name).parts[0] in OWNERS
              and Path(name).suffix == '.md' and not mode & 0o111}
    documentation = guides | {str(parent) for name in guides for parent in Path(name).parents
                              if parent != Path('.')}
    for name in [*files, *directories]:
        require(Path(name).parts[0] in {'cases', 'lib', 'helpers', 'e2e'} or name in documentation,
                f'superseded owner E2E content: {name}')


def normalize_e2e_cases(stage, case_files, *, from_owners):
    """Expose owner cases/libs through the one public prepared-runner layout."""
    root = stage / "test/e2e"
    cases = root / "cases"
    expected = {name: owner for owner, names in case_files.items() for name in names}
    require(len(expected) == sum(map(len, case_files.values())), 'duplicate E2E case ID')
    if not from_owners:
        require(cases.is_dir(), 'release must contain flat product cases')
        entries = list(root.rglob('*'))
        require(all(not path.is_symlink() and (path.is_file() or path.is_dir()) for path in entries),
                'non-regular released E2E content')
        validate_e2e_layout({str(path.relative_to(root)): path.stat().st_mode for path in entries if path.is_file()},
                            (str(path.relative_to(root)) for path in entries if path.is_dir()))
        require(set(tree_files(cases)) == set(expected), 'release case files differ from pinned test sources')
        return expected
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
                require(name not in seen, f"duplicate E2E case ID: {name} ({seen.get(name)} and {owner})")
                seen[name] = owner
                shutil.copy2(source, cases / name)
        owner_lib = suite / "lib"
        if owner_lib.is_dir():
            target = root / "lib" / owner
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(owner_lib, target)
        require(seen.keys() & set(case_files[owner]) == set(case_files[owner]), f'missing pinned case files: {owner}')
        shutil.rmtree(suite)
    require(seen == expected, 'assembled case files differ from pinned test sources')
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
        # Historical runtime and sandboxer units can legitimately have distinct
        # compiler contexts. Preserve the exact baseline embedded bytes unless
        # this plan explicitly replaces the embedded input.
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
                require(name.startswith(("test/", "docs/", "guide/", "workbench/")), f"platform cannot own {name}")
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
    require(plan["schema"] == 2, "unsupported integration plan")
    require(re.fullmatch(r"[0-9a-f]{40}", plan["framework_sha"]), "missing exact framework revision")
    require(set(plan["lanes"]) == set(ARCHES), "plan must contain both architectures")
    contract = plan['baseline'].get('delivery', 'historical')
    require(contract in ('historical', 'workbench-v1'), 'unsupported aggregate delivery contract')
    if contract == 'workbench-v1':
        baseline = plan['baseline']
        expected = {'SHA256SUMS', 'platform-' + baseline['version'] + '.tar.gz'}
        expected.update(archive_name(unit, record['version'], arch)
                        for unit, record in baseline['units'].items() for arch in ARCHES)
        expected.update(f'workbench-{arch}-{baseline["version"].removeprefix("release-")}.tar.gz' for arch in ARCHES)
        names = [record['name'] for record in baseline['assets']]
        require(set(names) == expected and len(names) == len(expected), 'new aggregate lacks its complete declared assets')
    case_files = plan["case_files"]
    require(set(plan["test_overlays"]) == (set(OWNERS) if plan["mode"] == "source" else set()),
            "test overlays differ from the exact input mode")
    for owner, names in case_files.items():
        require(owner in OWNERS and isinstance(names, list) and names and names == sorted(set(names)),
                f"invalid candidate case list for {owner}")
        for name in names:
            case_name(name)
    for arch, lane in plan["lanes"].items():
        require(lane["products"] == sorted(set(lane["products"])) and set(lane["products"]) <= set(PRODUCTS),
                "invalid affected product set")
        require(lane.get("embedded_products", []) in ([], ["envd"]), "unknown embedded product")
        require(lane["selection"] == suite_selection(plan["owners"], arch, case_files), "suite selection differs from trusted source filenames")
        expected_perf = ["working-set-smoke"] if plan["mode"] == "source" and arch == "x86_64" and set(plan["owners"]) & {"platform", "sandboxer"} else []
        require(lane["performance"] == expected_perf, "performance checks differ from trusted mode/owner selection")
    validate_test_revisions(plan.get("test_revisions"))
    return identity(plan)


def compose(plan, arch, assets, delta, output):
    plan_id = check_plan(plan)
    require(arch in ARCHES and output.name == arch and not output.exists(),
            "prepare needs a fresh, architecture-named destination")
    lane = plan["lanes"][arch]
    baseline = plan["baseline"]
    case_files = plan.get("case_files", {})
    expected_assets = {archive_name(unit, record["version"], arch): unit
                       for unit, record in baseline["units"].items()}
    require(set(baseline["units"]) == set(UNITS), "incomplete aggregate unit selection")
    expected_assets[f"platform-{baseline['version']}.tar.gz"] = "platform"
    records = {record["name"]: record for record in baseline["assets"]}
    require(len(records) == len(baseline["assets"]) and set(expected_assets) <= set(records),
            "aggregate lacks target assets; explicit ARM initialization is required")
    # Validate everything before applying candidate overlays. No latest lookup or
    # source-build fallback is available at this boundary.
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
    helpers = planned_helpers(lane["selection"]) if plan["mode"] == "source" else {}
    require(set(metadata.get("helpers", {})) == set(helpers), "test helper selection differs from plan")
    expected_delta = {"outputs.json"}
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
        for name in case_files[owner]:
            entry = overlay_case_path(owner, name)
            require(entry in files, f"missing pinned case: {owner}/{name}")
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
        if plan["mode"] == "source":
            shutil.rmtree(stage / "test/e2e")
            for owner in plan["test_overlays"]:
                source = delta / test_overlay_root(owner)
                if owner == "platform":
                    for path in list((stage / "test").rglob("*")):
                        relative_path = str(path.relative_to(stage / "test"))
                        if path.is_file() and platform_test_path(relative_path):
                            path.unlink()
                    shutil.copytree(source, stage / "test", dirs_exist_ok=True)
                else:
                    shutil.copytree(source, stage / "test/e2e" / owner)
        normalize_e2e_cases(stage, case_files, from_owners=plan["mode"] == "source")
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
        for case in lane["selection"]["cases"]:
            path = stage / "test/e2e/cases" / case_name(case)
            require(path.is_file(), f"missing selected test entry: {case}")
        for directory in ("images", "manifests", "fixtures"):
            (stage / directory).mkdir(exist_ok=True)
        helper_records = metadata.get("helpers", {})
        if plan["mode"] == "exact-assets":
            package = stage / "test/e2e/helpers" / arch
            declared = json.loads((package / "helpers.json").read_text())
            require(declared["arch"] == arch and declared["test_revisions"] ==
                    {owner: record["sha"] for owner, record in plan["test_revisions"].items() if owner != "platform"},
                    "packaged helper test pins differ from the release plan")
            helper_records = {}
            for name, owner in planned_helpers(lane["selection"]).items():
                record = declared["helpers"][name]
                revision = declared["framework_sha"] if owner == "framework" else plan["test_revisions"][owner]["sha"]
                require(record["sha256"] == digest(package / name) and record["source_sha"] == revision,
                        f"packaged helper identity mismatch: {name}")
                check_architecture(package / name, arch)
                (stage / "fixtures/bin").mkdir(exist_ok=True)
                shutil.copy2(package / name, stage / "fixtures/bin" / name)
                helper_records[name] = record
        elif helpers:
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
        provenance = {"schema": 2, "plan_id": plan_id, "arch": arch, "framework_sha": plan["framework_sha"],
                      "baseline": baseline, "products": products, "test_revisions": plan["test_revisions"],
                      "build_context": metadata["build_context"], "selection": lane["selection"],
                      "embedded": embedded, "helpers": helper_records,
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
    require(provenance["selection"] == plan["lanes"][arch]["selection"], "selected cases changed after preparation")
    require(provenance.get("test_revisions") == plan["test_revisions"], "prepared test pins differ from plan")
    return provenance


def workbench_cases(case_files, arch):
    cases = set(suite_selection(['platform'], arch, case_files)['cases'])
    if arch == 'aarch64':
        extra = {'sandbox.lifecycle.sh', 'snapshot.restore.sh', 'network.tapfd.sh'}
        require(extra <= set().union(*(set(names) for names in case_files.values())), 'missing declared ARM workbench KVM/network cases')
        cases |= extra
    return sorted(cases)


def check_workbench_results(version, source_sha, results, *, expected_assets, case_files):
    """Bind system/offline evidence to each tested image and outer archive."""
    require(isinstance(results, dict) and set(results) == set(ARCHES), 'both workbench architecture results are required')
    for arch, record in results.items():
        name = f'workbench-{arch}-{version.removeprefix("release-")}.tar.gz'
        require(record.get('conclusion') == 'success' and record.get('arch') == arch
                and record.get('aggregate_version') == version and record.get('source_revision') == source_sha,
                'workbench validation has another source/version/architecture or did not pass')
        require(record.get('archive') == name and 'sha256:' + record.get('sha256', '') == expected_assets.get(name)
                and re.fullmatch(r'sha256:[0-9a-f]{64}', record.get('image_id', '')),
                'workbench validation differs from staged archive/image identity')
        require(type(record.get('size')) is int and 0 < record['size'] < 2 * 1024**3,
                'workbench compressed archive must be smaller than 2 GiB')
        scope = record.get('qualification_scope')
        require(scope == 'system' or (arch == 'aarch64' and scope == 'artifact-only'),
                'workbench requires an explicit supported qualification scope')
        require(record.get('imported_image_id') == record['image_id']
                and record.get('release_inputs_verified') is True,
                'workbench archive import and release input verification are required')
        require(record.get('input_assets') == expected_assets, 'workbench input assets differ from the aggregate')
        for key in ('compression_seconds', 'import_seconds', 'wall_seconds',
                    *(('start_seconds',) if scope == 'system' else ())):
            value = record.get(key)
            require(type(value) in (int, float) and math.isfinite(value) and value >= 0,
                    'missing or invalid workbench measurement: ' + key)
        disk = record.get('disk', {})
        require(type(record.get('retained_task_bytes')) is int and record['retained_task_bytes'] >= 0
                and all(type(disk.get(key)) is int and disk[key] >= 0 for key in ('used_before', 'peak_used', 'peak_increase'))
                and disk['peak_used'] >= disk['used_before']
                and disk['peak_increase'] == disk['peak_used'] - disk['used_before'],
                'missing or inconsistent workbench disk measurements')
        if scope == 'artifact-only':
            require(record.get('cases') == [] and record.get('planned_cases') == workbench_cases(case_files, arch),
                    'artifact-only qualification must distinguish planned cases from execution')
            require(not any(key in record for key in ('offline', 'empty_private_daemon', 'isolation', 'preflight',
                        'apparmor', 'timings', 'provenance_sha256', 'start_seconds', 'preparation_network', 'execution_network')),
                    'artifact-only qualification cannot claim system or offline execution')
            continue
        require(record.get('offline') is True and record.get('empty_private_daemon') is True,
                'workbench requires offline preparation from empty private Docker state')
        require(record.get('preparation_network') == 'none' and record.get('execution_network') == 'owned-bridge',
                'workbench must block preparation fetches and retain the real guest-egress execution gate')
        require(record.get('isolation', {}).get('complete') is True
                and record['isolation'].get('image') == record['image_id'], 'workbench isolation did not pass on the tested image')
        preflight = record.get('preflight', {})
        require(all(preflight.get(key) == value for key, value in {
            'arch': arch, 'page_size': 4096, 'kvm': 'api-12', 'tun': 'create-close',
            'uffd': 'api-ioctl', 'bpf': 'create-pin-remove'}.items()), 'workbench native system preflight did not pass')
        require(preflight.get('docker', {}).get('driver') == 'overlay2'
                and preflight['docker'].get('root') == '/var/lib/docker'
                and preflight.get('containerd') == {'root': '/var/lib/containerd', 'state': '/run/containerd'},
                'workbench daemons do not use their private storage')
        require(record.get('cases') == workbench_cases(case_files, arch) and record.get('timings'),
                'workbench cases differ from the complete declared selection')
        check_timings(record['timings'], record['cases'])
        require(re.fullmatch(r'[0-9a-f]{64}', record.get('provenance_sha256', '')),
                'missing workbench prepared-workspace provenance digest')
    return results


def check_registry_binding(version, binding):
    registry = binding.get('registry', {})
    require(registry.get('reference') == 'ghcr.io/kuasar-sandbox/workbench:' + version.removeprefix('release-')
            and re.fullmatch(r'sha256:[0-9a-f]{64}', registry.get('digest', '')), 'missing aggregate registry identity')
    records = registry.get('architectures', {})
    require(set(records) == set(ARCHES), 'registry must select both native architectures')
    for arch, row in records.items():
        result = binding['workbench'][arch]
        require(re.fullmatch(r'sha256:[0-9a-f]{64}', row.get('digest', ''))
                and isinstance(row.get('size'), int) and row['size'] > 0
                and row.get('image_id') == result['image_id']
                and row.get('archive_sha256') == result['sha256'], 'registry/offline workbench identity differs')
    return registry


def collect_results(plan, results):
    require(set(results) == set(ARCHES), "both architecture results are required")
    for arch in ARCHES:
        record = results[arch]
        require(record["plan_id"] == check_plan(plan) and record["arch"] == arch,
                "result belongs to a different plan or architecture")
        require(record["conclusion"] == "success" and record["selection"] == plan["lanes"][arch]["selection"],
                f"selected {arch} validation did not pass")
        require(record.get("test_revisions") == plan["test_revisions"], "result test pins differ from plan")
        shards = dict(record['shards'])
        if record.get('performance') is not None:
            shards['performance'] = record['performance']
        require(record == collect_shard_results(plan, arch, shards), 'architecture execution evidence differs from plan')
    return {"plan_id": identity(plan), "test_revisions": plan["test_revisions"], "architectures": results}


def check_timings(timings, cases):
    require(isinstance(timings, list) and [record['case'] for record in timings] == cases,
            'missing or duplicated case execution evidence')
    require(all(record['exit_code'] == 0 and isinstance(record['wall_seconds'], (int, float)) and
                record['wall_seconds'] >= 0 for record in timings), 'case execution did not pass')


def requires_clean_runtime(arch, shard):
    return shard in {'storage', 'snapshot'} or (arch == 'aarch64' and shard == 'image')


def check_clean_environment(environment):
    require(isinstance(environment, dict) and environment.get('kind') == 'clean-container' and
            environment.get('verified') is True and environment.get('compilers') == [] and
            environment.get('component_source_trees') == [] and
            re.fullmatch(r'sha256:[0-9a-f]{64}', environment.get('image_id', '')),
            'missing source/compiler-free execution evidence')


def collect_shard_results(plan, arch, results):
    expected = shards(plan["lanes"][arch]["selection"])
    performance = plan['lanes'][arch]['performance']
    require(set(results) == set(expected) | ({'performance'} if performance else set()),
            f"missing or unexpected {arch} validation results")
    prepared = set()
    for shard, cases in expected.items():
        result = results[shard]
        require(result["arch"] == arch and result["shard"] == shard and result["plan_id"] == check_plan(plan),
                "shard result has the wrong immutable input identity")
        require(result["conclusion"] == "success" and result["cases"] == cases,
                "selected shard validation did not pass")
        check_timings(result['timings'], cases)
        if cases:
            check_clean_environment(result.get('preparation_environment'))
        if requires_clean_runtime(arch, shard):
            check_clean_environment(result.get('environment'))
        require(result.get("test_revisions") == plan["test_revisions"], "shard test pins differ from plan")
        require(re.fullmatch(r"[0-9a-f]{64}", result["provenance_sha256"]), "missing prepared input identity")
        prepared.add(result["provenance_sha256"])
    if performance:
        result = results['performance']
        require(result['arch'] == arch and result['plan_id'] == identity(plan) and
                result['test_revisions'] == plan['test_revisions'] and result['conclusion'] == 'success' and
                result['checks'] == performance, 'independent performance gate did not pass')
        check_timings(result['timings'], performance)
        prepared.add(result['provenance_sha256'])
    require(len(prepared) == 1, "shards executed different prepared workspaces")
    return {"arch": arch, "plan_id": identity(plan), "conclusion": "success",
            "selection": plan["lanes"][arch]["selection"], "shards": {key: results[key] for key in expected},
            "performance": results.get('performance'), "provenance_sha256": prepared.pop(),
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
