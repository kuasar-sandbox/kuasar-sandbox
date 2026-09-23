#!/usr/bin/env python3
"""Resolve one exact published aggregate and admitted candidate product delta."""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import quote

import artifacts

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("preview_coordinator", ROOT / "release/preview_coordinator.py")
release = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = release
spec.loader.exec_module(release)
PLATFORM = "kuasar-sandbox/kuasar-sandbox"
REPOSITORIES = {owner: f"kuasar-sandbox/{owner}" for owner in artifacts.OWNERS if owner != "platform"}
REPOSITORIES["platform"] = PLATFORM
LIBRARIES = {
    "accelerator": (), "connector": (), "sandboxer": ("accelerator", "connector"),
    "guest-runtime": ("accelerator",), "orchestrator": ("accelerator", "connector", "sandboxer"),
}
PROFILE_BINDING = re.compile(r"<!-- kuasar-integration-validation (\{[^\r\n]*\}) -->")


def source_text(repository, sha, path):
    value = release.api(f"repos/{repository}/contents/{path}?ref={quote(sha, safe='')}")
    artifacts.require(value.get("encoding") == "base64" and value.get("type") == "file", "expected exact source file")
    return base64.b64decode(value["content"]).decode()


def unit_owner(unit):
    return "guest-runtime" if unit in ("runtime", "vmlinux") else unit


def exact_sha(value):
    artifacts.require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value), "expected exact source SHA")
    return value


def public(repository):
    state = release.api(f"repos/{repository}")
    artifacts.require(state.get("full_name") == repository and state.get("visibility") == "public"
                      and state.get("private") is False, f"public artifact lane cannot qualify a non-public caller/companion: {repository}")


def candidate_case_names(repository, sha, owner):
    """Read the flat rewritten case set from the exact admitted candidate."""
    path = "test/e2e/platform/cases" if owner == "platform" else "test/e2e/cases"
    listing = release.api_optional(f"repos/{repository}/contents/{path}?ref={quote(sha, safe='')}")
    if listing is None:
        return []
    artifacts.require(isinstance(listing, list), f"candidate E2E cases are not a directory: {owner}")
    names = []
    for entry in listing:
        artifacts.require(entry.get("type") == "file" and isinstance(entry.get("name"), str),
                          f"candidate E2E cases must be flat files: {owner}")
        names.append(artifacts.case_name(entry["name"]))
    names.sort()
    artifacts.require(names == sorted(set(names)), f"duplicate candidate E2E case ID: {owner}")
    return names


def aggregate(version, *, require_dual=True):
    """Historical x86-only releases remain valid; normal dual lanes need ARM."""
    state = release.api_optional(f"repos/{PLATFORM}/releases/tags/{version}")
    if state is None:
        return None
    sha = release.tag_sha(PLATFORM, version)
    if state.get("draft") is not False or sha is None:
        return None
    artifacts.require(state.get("tag_name") == version and state.get("target_commitish") == sha
                      and state.get("prerelease") == ("-preview." in version), "aggregate release identity mismatch")
    relative = "releases/daily-preview.yaml" if "-preview." in version else "releases/release.yaml"
    manifest = source_text(PLATFORM, sha, relative)
    selected, _, units = release.selection.parse_manifest(manifest, f"{sha}:{relative}", "-preview." in version)
    artifacts.require(selected == version, "aggregate tag does not select its published version")
    base_names = {"SHA256SUMS", f"platform-{version}.tar.gz"}
    x86 = {artifacts.archive_name(unit, tag, "x86_64") for unit, tag in units.items()}
    arm = {artifacts.archive_name(unit, tag, "aarch64") for unit, tag in units.items()}
    names = [item["name"] for item in state["assets"]]
    artifacts.require(len(names) == len(set(names)), "duplicate aggregate asset names")
    if set(names) not in (base_names | x86, base_names | x86 | arm):
        return None
    for asset in state["assets"]:
        artifacts.require(asset.get("state") == "uploaded" and isinstance(asset.get("size"), int)
                          and asset["size"] > 0 and re.fullmatch(r"sha256:[0-9a-f]{64}", asset.get("digest") or ""),
                          "aggregate has an unfinished or unverified asset")
    runs = [run for run in release.aggregate_runs(version)
            if run.get("conclusion") == "success" and run.get("status") == "completed"
            and run.get("display_title") == release.aggregate_run_title(version, sha)]
    if not runs:
        return None  # An interrupted current publication may use its declared predecessor.
    bindings = PROFILE_BINDING.findall(state.get("body") or "")
    validation = {"x86_64": {"name": "historical-real-kvm"}}
    tests = None
    if arm <= set(names):
        artifacts.require(len(bindings) == 1, "dual aggregate lacks its declared validation profiles")
        binding = json.loads(bindings[0])
        artifacts.require(binding["aggregate_sha"] == sha and set(binding["architectures"]) == set(artifacts.ARCHES),
                          "aggregate validation binding has the wrong source or architecture set")
        expected = {asset["name"]: asset["digest"] for asset in state["assets"] if asset["name"] != "SHA256SUMS"}
        artifacts.require(binding["assets"] == expected, "aggregate bytes differ from validated bytes")
        pins = release.selection.test_revisions(release.selection.read_simple_yaml(manifest, relative), relative)
        tests = artifacts.release_test_revisions(pins, sha)
        artifacts.require(binding.get("test_revisions") == tests, "published test pins differ from committed selection")
        for arch, result in binding["architectures"].items():
            artifacts.require(result["arch"] == arch and result["conclusion"] == "success"
                              and result["profile"] == artifacts.profiles(["platform"], arch)
                              and result.get("test_revisions") == tests,
                              "aggregate did not pass its predeclared architecture profile")
        validation = binding["architectures"]
    elif require_dual:
        raise ValueError(f"{version} is a valid historical x86-only baseline; explicit ARM initialization and new unit versions are required")
    unit_records = {}
    for unit, tag in units.items():
        repository = REPOSITORIES[unit_owner(unit)]
        unit_records[unit] = {"version": tag, "repository": repository, "sha": exact_sha(release.tag_sha(repository, tag))}
    return {"repository": PLATFORM, "version": version, "sha": sha, "release_id": state["id"],
            "units": unit_records, "validation": validation, "test_revisions": tests,
            "validation_run": max(runs, key=lambda run: run["id"])["html_url"],
            "assets": [{key: asset[key] for key in ("id", "name", "size", "digest")} for asset in state["assets"]]}


def baseline(platform_sha, base_ref):
    preview = base_ref == "main"
    relative = "releases/daily-preview.yaml" if preview else "releases/release.yaml"
    version, previous, _ = release.selection.parse_manifest(source_text(PLATFORM, platform_sha, relative), relative, preview)
    # Only maintained selections may recover an interrupted publication. Never
    # select each unit's latest release or silently switch to source builds.
    for selected in (version, previous):
        if selected:
            result = aggregate(selected)
            if result is not None:
                return result
    raise ValueError("neither the maintained aggregate nor its explicit predecessor is a published validated baseline")


def changed_files(repository, base, candidate):
    comparison = release.api(f"repos/{repository}/compare/{base}...{candidate}")
    files = comparison.get("files", [])
    if len(files) < 300 and comparison.get("merge_base_commit", {}).get("sha") == base:
        paths = {file["filename"] for file in files}
        paths.update(file["previous_filename"] for file in files if "previous_filename" in file)
    else:
        # GitHub truncates compare file lists at 300. Read Git trees without a
        # checkout or candidate execution; do not under-select the product delta.
        with tempfile.TemporaryDirectory(prefix="integration-diff-") as directory:
            subprocess.run(["git", "init", "--bare", "--quiet", directory], check=True)
            subprocess.run(["git", "-C", directory, "fetch", "--quiet", "--filter=blob:none", "--depth=2",
                            f"https://github.com/{repository}.git", base, candidate], check=True)
            names = subprocess.check_output(["git", "-C", directory, "diff", "--name-only", "-z", base, candidate])
            paths = set(names.decode().rstrip("\0").split("\0")) - {""}
    for path in paths:
        artifacts.relative(path)
    return sorted(paths)


def product_source_map(products, sources, kernel_sha):
    result = {}
    for name in products:
        owner = unit_owner(artifacts.PRODUCTS[name])
        dependencies = {owner, *LIBRARIES[owner]}
        if name == "sandbox-init":
            dependencies = {"sandboxer"}
        if name == "sandbox-runtime.bundle":
            dependencies |= {"sandboxer"}
        if name in ("mkfs.erofs", "vmlinux"):
            dependencies = {"guest-runtime"}
        result[name] = {REPOSITORIES[dependency]: sources[dependency]["sha"] for dependency in sorted(dependencies)}
        if name == "vmlinux":
            result[name][REPOSITORIES["guest-runtime"]] = kernel_sha
    return result


def source_plan(framework_sha):
    primary = {"repository": os.environ["CANDIDATE_REPOSITORY"], "candidate_sha": os.environ["CANDIDATE_SHA"],
               "base_sha": os.environ["CANDIDATE_BASE_SHA"], "head_sha": os.environ["CANDIDATE_HEAD_SHA"],
               "base_ref": os.environ["CANDIDATE_BASE_REF"], "pull_request_number": int(os.environ["CANDIDATE_PR"])}
    records = [primary, *json.loads(os.environ["COMPANION_CANDIDATES"])]
    for record in records:
        public(record["repository"])
    platform_record = next((record for record in records if record["repository"] == PLATFORM), None)
    base_ref = platform_record["base_ref"] if platform_record else primary["base_ref"]
    platform_sha = platform_record["base_sha"] if platform_record else exact_sha(release.branch_sha(PLATFORM, base_ref))
    selected = baseline(platform_sha, base_ref)
    sources = {owner: {"repository": repository, "sha": selected["sha"] if owner == "platform" else
                      selected["units"]["runtime" if owner == "guest-runtime" else owner]["sha"], "role": "baseline"}
               for owner, repository in REPOSITORIES.items()}
    tests = {owner: dict(record) for owner, record in
             artifacts.validate_test_revisions(selected.get("test_revisions")).items()}
    changes, owners, overlays, candidate_cases = {}, [], [], {}
    kernel_sha = selected["units"]["vmlinux"]["sha"]
    for record in records:
        owner = "platform" if record["repository"] == PLATFORM else record["repository"].split("/")[1]
        if base_ref != "main" and owner != "platform":
            unit = "runtime" if owner == "guest-runtime" else owner
            expected_ref = release.preview_selection.component_source_ref(base_ref, unit, selected["units"][unit]["version"])
            artifacts.require(record["base_ref"] == expected_ref, "candidate targets a different release line")
        changes[owner] = changed_files(record["repository"], sources[owner]["sha"], record["candidate_sha"])
        sources[owner] = {"repository": record["repository"], "sha": exact_sha(record["candidate_sha"]),
                          "role": "candidate" if record is primary else "companion"}
        tests[owner] = dict(sources[owner])
        names = candidate_case_names(record["repository"], record["candidate_sha"], owner)
        if names:
            candidate_cases[owner] = names
        owners.append(owner)
        overlays.append(owner)
        if owner == "guest-runtime":
            kernel_sha = record["candidate_sha"]
    products = artifacts.changed_products(changes)
    # Reuse #150/#151's Makefile input projection instead of treating a change
    # to an unrelated native target as a kernel change.
    guest_changes = changes.get("guest-runtime", [])
    # Kernel is an independent release unit: its baseline source can differ
    # from runtime's source even though both live in guest-runtime.
    if "guest-runtime" in owners:
        kernel_changes = changed_files(REPOSITORIES["guest-runtime"], selected["units"]["vmlinux"]["sha"], kernel_sha)
        kernel_products = artifacts.changed_products({"guest-runtime": kernel_changes})
        products = sorted((set(products) - {"vmlinux"}) | ({"vmlinux"} if "vmlinux" in kernel_products else set()))
    else:
        kernel_changes = []
    if "vmlinux" in products and "native-deps/Makefile" in kernel_changes:
        kernel_paths = {"native-deps/deps/common.sh", "native-deps/deps/build-vmlinux.sh"}
        other_kernel = any(path in kernel_paths or path.startswith(("native-deps/deps/vmlinux/", "native-deps/deps/linux-patches/")) for path in kernel_changes)
        old = source_text(REPOSITORIES["guest-runtime"], selected["units"]["vmlinux"]["sha"], "native-deps/Makefile")
        new = source_text(REPOSITORIES["guest-runtime"], sources["guest-runtime"]["sha"], "native-deps/Makefile")
        if not other_kernel and release.preview_selection.vmlinux_make_inputs(old) == release.preview_selection.vmlinux_make_inputs(new):
            products.remove("vmlinux")
    embedded = ["envd"] if any(path in ("native-deps/deps/build-envd.sh", "native-deps/deps/common.sh", "native-deps/Makefile") for path in guest_changes) else []
    plan = {"schema": 1, "mode": "source", "framework_sha": exact_sha(framework_sha), "baseline": selected,
            "candidate_records": records, "owners": sorted(owners), "changes": changes,
            "sources": sources, "kernel_sha": kernel_sha,
            "test_revisions": tests, "test_overlays": sorted(overlays), "candidate_cases": candidate_cases,
            "product_sources": product_source_map(products, sources, kernel_sha),
            "embedded_sources": {"envd": {REPOSITORIES["guest-runtime"]: sources["guest-runtime"]["sha"]}},
            "lanes": {arch: {"products": products, "embedded_products": embedded,
                             "extra_checks": {"sandboxer": ["working-set-smoke"]} if arch == "x86_64" and set(owners) & {"platform", "sandboxer"} else {},
                             "profile": artifacts.profiles(owners, arch, candidate_cases)} for arch in artifacts.ARCHES}}
    artifacts.check_plan(plan)
    return plan


def exact_assets_plan(framework_sha, stage):
    """A staged aggregate is a candidate, never a published baseline fallback."""
    version, sha = os.environ["RELEASE_VERSION"], exact_sha(os.environ["PLATFORM_SOURCE_SHA"])
    public(PLATFORM)
    relative = "releases/daily-preview.yaml" if "-preview." in version else "releases/release.yaml"
    manifest = source_text(PLATFORM, sha, relative)
    selected, _, units = release.selection.parse_manifest(manifest, relative, "-preview." in version)
    pins = release.selection.test_revisions(release.selection.read_simple_yaml(manifest, relative), relative)
    tests = artifacts.release_test_revisions(pins, sha)
    artifacts.require(json.loads((stage / "test-revisions.json").read_text()) == pins,
                      "staged test pins differ from committed selection")
    artifacts.require(selected == version, "staged aggregate is not selected by its exact platform source")
    artifacts.require((stage / "selection.tsv").read_text() == "".join(f"{unit}\t{units[unit]}\n" for unit in release.selection.UNITS),
                      "staged selection differs from exact source")
    names = {"SHA256SUMS", f"platform-{version}.tar.gz"} | {
        artifacts.archive_name(unit, tag, arch) for unit, tag in units.items() for arch in artifacts.ARCHES}
    files = artifacts.tree_files(stage / "assets")
    artifacts.require(set(files) == names, "a new aggregate must explicitly stage both architecture asset sets")
    unit_records = {}
    for unit, tag in units.items():
        repository = REPOSITORIES[unit_owner(unit)]
        public(repository)
        unit_records[unit] = {"version": tag, "repository": repository, "sha": exact_sha(release.tag_sha(repository, tag))}
    baseline = {"repository": PLATFORM, "version": version, "sha": sha, "units": unit_records, "staged": True,
                "assets": [{"name": name, "size": (stage / "assets" / name).stat().st_size,
                            "digest": "sha256:" + value} for name, value in sorted(files.items())]}
    sources = {owner: {"repository": repository, "sha": sha if owner == "platform" else
                      unit_records["runtime" if owner == "guest-runtime" else owner]["sha"], "role": "release"}
               for owner, repository in REPOSITORIES.items()}
    plan = {"schema": 1, "mode": "exact-assets", "framework_sha": exact_sha(framework_sha), "baseline": baseline,
            "candidate_records": [], "owners": ["platform"], "sources": sources, "kernel_sha": unit_records["vmlinux"]["sha"],
            "test_revisions": tests, "test_overlays": [], "candidate_cases": {}, "product_sources": {}, "embedded_sources": {},
            "lanes": {arch: {"products": [], "embedded_products": [], "profile": artifacts.profiles(["platform"], arch)}
                      for arch in artifacts.ARCHES}}
    artifacts.check_plan(plan)
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--framework-sha", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--stage", type=Path)
    args = parser.parse_args()
    # The existing event, exact-merge-parent and companion checks remain the
    # admission authority. This command is run only after those checks succeed.
    plan = exact_assets_plan(args.framework_sha, args.stage) if args.stage else source_plan(args.framework_sha)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as output:
        output.write(artifacts.canonical(plan) + b"\n")
    print(f"resolved aggregate {plan['baseline']['version']} plan={artifacts.identity(plan)}")


if __name__ == "__main__":
    main()
